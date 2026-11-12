"""The background service's pure parts: the switch's systemctl commands and the replug debounce."""
import itertools
import unittest

from booth import service


class Switch(unittest.TestCase):
    def test_on_enables_and_starts_the_service(self):
        for in_service in (False, True):
            self.assertEqual(service.switch(True, in_service), ["systemctl", "--user", "enable", "--now", "booth.service"])

    def test_off_from_a_plain_instance_disables_and_stops_it(self):
        self.assertEqual(service.switch(False, False), ["systemctl", "--user", "disable", "--now", "booth.service"])

    def test_off_from_inside_the_service_only_disables(self):
        # Stopping would kill the window the switch is in: the app lets go of its hold instead and exits when the
        # window closes, a clean exit that Restart=on-failure leaves stopped.
        self.assertEqual(service.switch(False, True), ["systemctl", "--user", "disable", "booth.service"])

    def test_is_enabled_query(self):
        self.assertEqual(service.IS_ENABLED, ["systemctl", "--user", "is-enabled", "--quiet", "booth.service"])

    def test_run_reports_the_exit_status(self):
        self.assertTrue(service.run(["true"]))
        self.assertFalse(service.run(["false"]))
        self.assertFalse(service.run(["/nonexistent/systemctl"]))


class Timers:
    """GLib.timeout_add / source_remove stand-ins: pending callbacks by source id."""

    def __init__(self):
        self.pending, self.ids, self.removed = {}, itertools.count(1), []

    def add(self, ms, callback):
        sid = next(self.ids)
        self.pending[sid] = (ms, callback)
        return sid

    def remove(self, sid):
        self.removed.append(sid)
        del self.pending[sid]  # KeyError: removing a source that already ran (GLib warns)

    def fire(self):
        (sid, (_, callback)), = self.pending.items()
        del self.pending[sid]
        return callback()


class DebounceTest(unittest.TestCase):
    def setUp(self):
        self.timers, self.fired = Timers(), []
        self.d = service.Debounce(2000, lambda: self.fired.append(1), self.timers.add, self.timers.remove)

    def test_ten_fast_events_fire_once_after_the_last(self):
        for _ in range(10):
            self.d.poke()
        self.assertEqual(len(self.timers.pending), 1)
        self.assertEqual(self.fired, [])
        self.assertIs(self.timers.fire(), False)  # GLib.SOURCE_REMOVE: a one-shot timer
        self.assertEqual(self.fired, [1])

    def test_each_event_restarts_the_wait(self):
        self.d.poke()
        self.d.poke()
        self.assertEqual(self.timers.removed, [1])
        self.assertEqual(list(self.timers.pending), [2])
        self.assertEqual(self.timers.pending[2][0], 2000)

    def test_an_event_after_the_fire_starts_a_new_wait_and_never_removes_the_spent_timer(self):
        self.d.poke()
        self.timers.fire()
        self.d.poke()
        self.timers.fire()
        self.assertEqual(self.fired, [1, 1])
        self.assertEqual(self.timers.removed, [])


if __name__ == "__main__":
    unittest.main()
