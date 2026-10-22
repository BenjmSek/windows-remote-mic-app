"""Chromecast service lifecycle; receiver ownership stays in Client.

Application services provide shared input and status operations. This owner keeps
its receiver and voice host until both confirm cleanup, preserving switch safety.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable, Any
from . import bridge_runtime_status, diagnostic_trace


# Confirming cleanup is the safe path, but waiting for it forever is not: the
# desktop stop window is 5 seconds, so an unconfirmed stage that outlives it
# makes every exit report "遥控器服务没有在限定时间内停止" even when the only
# fault is one stalled resource. Both budgets together stay well inside the
# 5 second stop window the desktop exit path allows.
CLEANUP_GIVE_UP_SECONDS = 2.0
RELEASE_INPUTS_GIVE_UP_SECONDS = 1.0


@dataclass(frozen=True)
class RuntimeServices:
    selected_key: Callable[[], str]
    logger: Any
    create_voice_host: Callable
    set_input_state: Callable
    publish_status: Callable
    prepare_input_lifecycle: Callable
    start_hid_listener: Callable
    stop_input: Callable
    disable_input: Callable
    cancel_mappings: Callable
    release_inputs: Callable
    route_edge: Callable


class ChromecastRuntime:
    def __init__(self, services: RuntimeServices):
        self.services = services
        self.client = None
        self.voice_host = None
        self.stopping = threading.Event()

    async def stop(self):
        self.stopping.set()
        if self.voice_host is not None:
            await asyncio.to_thread(self.voice_host.stop)
        if self.client is not None:
            await asyncio.to_thread(self.client.stop)

    def cancel_input(self):
        if self.voice_host is not None:
            self.voice_host.request_stop()
        self.services.cancel_mappings()

    def on_edge(self, edge):
        if (self.client is None or edge.entity != self.services.selected_key()
                or edge.generation != self.client.identity.generation):
            return
        self.services.route_edge(edge)

    async def run(self) -> None:
        from .chromecast_client import Client, message
        self.services.set_input_state(raw_input_state="starting", hid_tap_state="starting",
                                      voice_key_physicalizer_state="not_used")
        self.services.publish_status(bridge_runtime_status.BridgeConnectionState.CONNECTING)
        self.voice_host = self.services.create_voice_host()
        self.client = Client(self.services.selected_key(), mode="run", on_edge=self.on_edge,
                                  on_lost=self.cancel_input,
                                  on_voice=self.voice_host.enqueue)
        self.voice_host.start(self.client)
        try:
            # Keyboard ownership is shared safety infrastructure, not the
            # remote decoder. Start it before accepting host shortcuts.
            self.services.prepare_input_lifecycle()
            self.services.start_hid_listener()
            await asyncio.to_thread(self.client.start, cancel_event=self.stopping)
            self.services.set_input_state(raw_input_state="chromecast_ready", hid_tap_state="ready")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.CONNECTED)
            while not self.stopping.is_set() and not self.client.finished.is_set():
                await asyncio.sleep(.05)
            if self.client.finished.is_set() and not self.stopping.is_set():
                raise RuntimeError(message(self.client.reason))
        except Exception:
            self.services.disable_input()
            self.services.set_input_state(raw_input_state="chromecast_" + self.client.reason,
                                          hid_tap_state="failed")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE)
            self.services.logger.warning("Chromecast receiver: stage=%s code=%s; %s",
                                 self.client.failure_stage, self.client.failure_code,
                                 message(self.client.reason))
            # Preserve a visible reason until the user stops/restarts; do not
            # automatically repeat permission prompts after a failed start.
            while not self.stopping.is_set():
                await asyncio.sleep(.1)
        finally:
            # Keep ownership on teardown failure; the desktop must not switch
            # entities over a receiver whose exit has not been confirmed.
            progress = diagnostic_trace.CleanupProgress(
                self.services.logger, "chromecast_runtime",
                getattr(getattr(self.client, "identity", None), "generation", "none")[:12])
            progress.update("cancel_input", "begin")
            self.cancel_input()
            progress.update("cancel_input", "done")
            cleanup_last_log = {}
            cleanup_deadline = time.monotonic() + CLEANUP_GIVE_UP_SECONDS
            stalled_stage = None
            while True:
                try:
                    for stage, cleanup in (("voice_host", self.voice_host.stop),
                                           ("receiver", self.client.stop),
                                           ("input_channels", self.services.stop_input)):
                        started = time.monotonic()
                        report = started - cleanup_last_log.get(stage, float("-inf")) >= 5
                        if report:
                            cleanup_last_log[stage] = started
                        progress.update(stage, "begin", report=report)
                        await asyncio.to_thread(cleanup)
                        progress.update(stage, "done", report=report)
                    break
                except RuntimeError as exc:
                    progress.update(stage, "retry", report=report)
                    if report:
                        self.services.logger.warning("Chromecast shutdown: stage=%s state=retry error_type=%s elapsed_ms=%.0f",
                                             stage, type(exc).__name__, (time.monotonic() - started) * 1000)
                    # Remain the live bridge owner, so stop/switch/exit cannot
                    # report success over an unconfirmed elevated receiver.
                    self.services.set_input_state(raw_input_state="failed_stopping")
                    if time.monotonic() >= cleanup_deadline:
                        # Stop owning the exit wait, not the failure: the stage
                        # stays unconfirmed and the input state keeps saying so.
                        stalled_stage = stage
                        progress.update(stage, "abandoned")
                        self.services.logger.error("Chromecast shutdown: stage=%s state=abandoned error_type=%s elapsed_ms=%.0f",
                                           stage, type(exc).__name__, (time.monotonic() - started) * 1000)
                        break
                    await asyncio.sleep(.25)
                except Exception as exc:
                    progress.update(stage, "failed")
                    self.services.logger.error("Chromecast shutdown: stage=%s state=error error_type=%s",
                                       stage, type(exc).__name__)
                    raise
            self.client = None
            self.voice_host = None
            progress.update("release_inputs", "begin")
            release_deadline = time.monotonic() + RELEASE_INPUTS_GIVE_UP_SECONDS
            release_started = time.monotonic()
            while True:
                if self.services.release_inputs():
                    break
                self.services.set_input_state(raw_input_state="failed_stopping")
                if time.monotonic() >= release_deadline:
                    stalled_stage = stalled_stage or "release_inputs"
                    progress.update("release_inputs", "abandoned")
                    self.services.logger.error("Chromecast shutdown: stage=release_inputs state=abandoned elapsed_ms=%.0f",
                                       (time.monotonic() - release_started) * 1000)
                    break
                await asyncio.sleep(.25)
            if stalled_stage is None:
                self.services.set_input_state(raw_input_state="stopped")
                progress.update("release_inputs", "done")
