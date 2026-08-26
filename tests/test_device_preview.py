import unittest


class DevicePreviewTests(unittest.TestCase):
    def test_owner_tuple_admission_and_foreign_conflict(self):
        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
            PreviewAdmissionStatus,
        )

        now = [100.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner(device_id="device-a", preview_instance="tab-a")
        accepted = manager.submit(
            DevicePreviewRequest(
                owner=owner,
                generation=1,
                config={"module": "clock_lava"},
                timeout_seconds=60,
            )
        )

        self.assertEqual(accepted.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertEqual(accepted.lease.owner, owner)
        self.assertEqual(accepted.lease.generation, 1)

        for foreign_owner in (
            DevicePreviewOwner(device_id="device-b", preview_instance="tab-a"),
            DevicePreviewOwner(device_id="device-a", preview_instance="tab-b"),
        ):
            rejected = manager.submit(
                DevicePreviewRequest(
                    owner=foreign_owner,
                    generation=2,
                    config={"module": "weather"},
                    timeout_seconds=30,
                )
            )
            self.assertEqual(rejected.status, PreviewAdmissionStatus.OWNER_CONFLICT)
            self.assertIsNone(rejected.lease)

        active = manager.active_lease()
        self.assertEqual(active, accepted.lease)
        self.assertEqual(active.request.config, {"module": "clock_lava"})
        self.assertEqual(active.content_deadline, 160.0)
        self.assertEqual(active.connectivity_deadline, 115.0)

    def test_explicitly_unbounded_preview_continues_while_heartbeats_arrive(self):
        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
            PreviewAdmissionStatus,
            PreviewHeartbeatStatus,
        )

        now = [100.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner("device-a", "preview-a")
        admitted = manager.submit(DevicePreviewRequest(
            owner, 1, {"modules": []}, timeout_seconds=None,
        ))
        self.assertEqual(admitted.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertIsNotNone(admitted.lease)
        self.assertEqual(admitted.lease.content_deadline, float("inf"))

        outcome = None
        for heartbeat_at in range(105, 175, 5):
            now[0] = float(heartbeat_at)
            outcome = manager.heartbeat(owner, generation=1)
            self.assertEqual(outcome.status, PreviewHeartbeatStatus.ACCEPTED)
            self.assertIsNotNone(outcome.lease)
            self.assertEqual(outcome.lease.content_deadline, float("inf"))

        self.assertIsNotNone(outcome)
        active = manager.active_lease()
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.generation, 1)

    def test_newer_generation_supersedes_and_stale_equal_do_not_mutate(self):
        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
            PreviewAdmissionStatus,
        )

        now = [200.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner(device_id="device-a", preview_instance="tab-a")

        first = manager.submit(
            DevicePreviewRequest(owner, 3, {"module": "clock_lava"}, 60)
        )
        original_lease = first.lease

        for generation in (3, 2):
            rejected = manager.submit(
                DevicePreviewRequest(owner, generation, {"module": "weather"}, 5)
            )
            self.assertEqual(
                rejected.status,
                PreviewAdmissionStatus.STALE_GENERATION,
            )
            self.assertIsNone(rejected.lease)
            self.assertIs(manager.active_lease(), original_lease)

        now[0] = 204.0
        replacement = manager.submit(
            DevicePreviewRequest(owner, 4, {"module": "weather"}, 30)
        )
        self.assertEqual(replacement.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertEqual(replacement.lease.generation, 4)
        self.assertEqual(replacement.lease.request.config, {"module": "weather"})
        self.assertEqual(replacement.lease.content_deadline, 234.0)
        self.assertEqual(replacement.lease.connectivity_deadline, 215.0)
        self.assertIs(manager.active_lease(), replacement.lease)
        self.assertEqual(original_lease.request.config, {"module": "clock_lava"})
        self.assertEqual(original_lease.content_deadline, 260.0)


    def test_accepted_update_restarts_timeout_but_other_traffic_does_not(self):
        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
            PreviewAdmissionStatus,
        )

        now = [300.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner(device_id="device-a", preview_instance="tab-a")
        first = manager.submit(
            DevicePreviewRequest(owner, 1, {"module": "clock_lava"}, 60)
        )
        self.assertEqual(first.lease.content_deadline, 360.0)
        self.assertEqual(first.lease.connectivity_deadline, 315.0)

        now[0] = 304.0
        foreign = manager.submit(
            DevicePreviewRequest(
                DevicePreviewOwner("device-b", "tab-a"),
                2,
                {"module": "weather"},
                30,
            )
        )
        stale = manager.submit(
            DevicePreviewRequest(owner, 1, {"module": "weather"}, 30)
        )
        self.assertEqual(foreign.status, PreviewAdmissionStatus.OWNER_CONFLICT)
        self.assertEqual(stale.status, PreviewAdmissionStatus.STALE_GENERATION)
        self.assertIs(manager.active_lease(), first.lease)

        accepted = manager.submit(
            DevicePreviewRequest(owner, 2, {"module": "weather"}, 30)
        )
        self.assertEqual(accepted.status, PreviewAdmissionStatus.ACCEPTED)
        self.assertEqual(accepted.lease.content_deadline, 334.0)
        self.assertEqual(accepted.lease.connectivity_deadline, 315.0)


    def test_heartbeat_five_seconds_renews_fifteen_second_connectivity_only(self):
        from unittest.mock import Mock

        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
            PreviewHeartbeatStatus,
        )

        now = [400.0]
        manager = DevicePreviewManager(clock=lambda: now[0])
        owner = DevicePreviewOwner("device-a", "tab-a")
        cleanup = Mock()
        admitted = manager.submit(
            DevicePreviewRequest(
                owner, 7, {"module": "clock_lava"}, 60, cleanup=cleanup,
            )
        )
        self.assertEqual(admitted.lease.content_deadline, 460.0)
        self.assertEqual(admitted.lease.connectivity_deadline, 415.0)

        now[0] = 404.0
        early = manager.heartbeat(owner, generation=7)
        self.assertEqual(early.status, PreviewHeartbeatStatus.TOO_EARLY)
        self.assertEqual(manager.active_lease().connectivity_deadline, 415.0)

        now[0] = 405.0
        renewed = manager.heartbeat(owner, generation=7)
        self.assertEqual(renewed.status, PreviewHeartbeatStatus.ACCEPTED)
        self.assertEqual(renewed.lease.content_deadline, 460.0)
        self.assertEqual(renewed.lease.connectivity_deadline, 420.0)

        now[0] = 410.0
        foreign = manager.heartbeat(
            DevicePreviewOwner("device-b", "tab-a"), generation=7
        )
        stale = manager.heartbeat(owner, generation=6)
        self.assertEqual(foreign.status, PreviewHeartbeatStatus.OWNER_CONFLICT)
        self.assertEqual(stale.status, PreviewHeartbeatStatus.STALE_GENERATION)
        self.assertEqual(manager.active_lease().connectivity_deadline, 420.0)

        now[0] = 420.0
        expired = manager.heartbeat(owner, generation=7)
        self.assertEqual(expired.status, PreviewHeartbeatStatus.EXPIRED)
        self.assertIsNone(expired.lease)
        self.assertIsNone(manager.active_lease())
        manager.close()
        cleanup.assert_called_once_with()


    def test_snapshot_discovered_expiry_uses_terminal_callback(self):
        from unittest.mock import Mock

        from device_preview import DevicePreviewManager, DevicePreviewOwner, DevicePreviewRequest

        now = [900.0]
        terminal = Mock()
        cleanup = Mock()
        manager = DevicePreviewManager(clock=lambda: now[0], on_expire=terminal)
        admitted = manager.submit(
            DevicePreviewRequest(
                DevicePreviewOwner("device-a", "tab-a"),
                1,
                {"module": "weather"},
                60,
                cleanup=cleanup,
            )
        )
        with manager._lock:
            manager._reaper_stop = True
            reaper = manager._reaper_thread
            manager._lock.notify_all()
        reaper.join(0.5)
        now[0] = admitted.lease.connectivity_deadline

        self.assertIsNone(manager.active_lease())

        terminal.assert_called_once_with(admitted.lease)
        cleanup.assert_called_once_with()
        manager.close()
        cleanup.assert_called_once_with()

    def test_heartbeat_discovered_expiry_uses_terminal_callback(self):
        from unittest.mock import Mock

        from device_preview import DevicePreviewManager, DevicePreviewOwner, DevicePreviewRequest

        now = [800.0]
        terminal = Mock()
        cleanup = Mock()
        manager = DevicePreviewManager(clock=lambda: now[0], on_expire=terminal)
        owner = DevicePreviewOwner("device-a", "tab-a")
        admitted = manager.submit(
            DevicePreviewRequest(
                owner, 1, {"module": "weather"}, 60, cleanup=cleanup,
            )
        )
        with manager._lock:
            manager._reaper_stop = True
            reaper = manager._reaper_thread
            manager._lock.notify_all()
        reaper.join(0.5)
        now[0] = admitted.lease.connectivity_deadline

        manager.heartbeat(owner, generation=1)

        terminal.assert_called_once_with(admitted.lease)
        cleanup.assert_called_once_with()
        manager.close()
        cleanup.assert_called_once_with()

    def test_content_expiry_is_autonomous_after_connectivity_renewal(self):
        import threading
        from unittest.mock import Mock

        from device_preview import (
            DevicePreviewManager, DevicePreviewOwner, DevicePreviewRequest,
            PreviewExpiryCause,
        )

        now = [700.0]
        expired = threading.Event()
        expired_leases = []
        cleanup = Mock()

        def expire(lease):
            expired_leases.append(lease)
            expired.set()

        manager = DevicePreviewManager(
            clock=lambda: now[0],
            on_expire=expire,
        )
        owner = DevicePreviewOwner("device-a", "tab-a")
        manager.submit(
            DevicePreviewRequest(
                owner, 1, {"module": "clock_lava"}, 6, cleanup=cleanup,
            )
        )
        now[0] = 705.0
        self.assertEqual(
            manager.heartbeat(owner, generation=1).lease.connectivity_deadline,
            720.0,
        )

        now[0] = 706.0
        self.assertTrue(expired.wait(0.5))
        self.assertIs(expired_leases[0].expiry_cause, PreviewExpiryCause.CONTENT)
        cleanup.assert_called_once_with()
        self.assertIsNone(manager.active_lease())
        manager.close()
        cleanup.assert_called_once_with()

    def test_supersession_failure_and_close_release_media_once(self):
        from unittest.mock import Mock
        from device_preview import (
            DevicePreviewManager,
            DevicePreviewOwner,
            DevicePreviewRequest,
        )

        manager = DevicePreviewManager(clock=lambda: 500.0)
        owner = DevicePreviewOwner("device-a", "tab-a")
        first_close = Mock()
        failed_close = Mock()
        final_close = Mock()

        manager.submit(
            DevicePreviewRequest(
                owner, 1, {"module": "media:first.gif"}, 60, cleanup=first_close,
            )
        )
        manager.submit(
            DevicePreviewRequest(
                owner, 2, {"module": "media:failed.gif"}, 60, cleanup=failed_close,
            )
        )
        first_close.assert_called_once_with()

        self.assertTrue(manager.fail(owner, generation=2))
        self.assertFalse(manager.fail(owner, generation=2))
        failed_close.assert_called_once_with()

        manager.submit(
            DevicePreviewRequest(
                owner, 3, {"module": "media:final.gif"}, 60, cleanup=final_close,
            )
        )
        manager.close()
        manager.close()
        final_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
