import importlib.util
import ipaddress
import ssl
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def load_worker():
    path = Path(__file__).resolve().parents[1] / "deploy/domain_worker.py"
    spec = importlib.util.spec_from_file_location("domain_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


w = load_worker()
IP = ipaddress.IPv4Address("192.0.2.50")
V6 = {ipaddress.IPv6Address("2606:4700:4700::1111")}
ROW = dict(id=1, hostname="alias.example.test", enabled=True, status="pending", updated_at=1)


class WorkerTests(unittest.TestCase):
    def reconciliation(self, row, resolution=({IP}, V6), result=None, update=True):
        db = MagicMock()
        db.execute.return_value.rowcount = int(update)
        with patch.object(w, "address_sets", return_value=resolution), patch.object(w, "check_tls", side_effect=result) as tls:
            w.reconcile(db, row, IP, V6, object())
        states = [call.args[1][0] for call in db.execute.call_args_list]
        self.assertTrue(all(call.args[1][-3:] == (row['id'],row['enabled'],row['updated_at'])
                            for call in db.execute.call_args_list))
        return states, tls.called

    def test_good_dual_stack(self):
        self.assertEqual(self.reconciliation(ROW), (["provisioning", "active"], True))

    def test_v4_only(self):
        self.assertEqual(self.reconciliation(ROW, ({IP},set())), (["provisioning", "active"], True))

    def test_stale_aaaa(self):
        self.assertEqual(self.reconciliation(ROW, ({IP},{ipaddress.IPv6Address("2001:db8::1")})), (["dns_error"], False))

    def test_wrong_a(self):
        self.assertEqual(self.reconciliation(ROW, ({IP,ipaddress.IPv4Address("192.0.2.5")},V6)), (["dns_error"], False))
        self.assertEqual(self.reconciliation(ROW, (set(),V6)), (["dns_error"], False))

    def test_activate_only_if_tls_succeeds(self):
        self.assertEqual(self.reconciliation(ROW,result=ssl.SSLError()),(["provisioning","tls_error"],True))

    def test_recheck_active_site(self):
        self.assertEqual(self.reconciliation({**ROW,"status":"active"}),(["active"],True))

    def test_cancellation(self):
        self.assertEqual(self.reconciliation(ROW,update=False), (["provisioning"],False))
        self.assertEqual(self.reconciliation({**ROW,"enabled":False}), (["disabled"],False))

    def test_invalid_hostname(self):
        for hostname in ("localhost","127.0.0.1","bad:443","wrong.example.test/dir"):
            self.assertEqual(self.reconciliation({**ROW,"hostname":hostname}),(["error"],False))

    def test_tls_always_goes_to_internal_service(self):
        ctx = MagicMock()
        with patch.object(w.socket,"create_connection") as dial:
            w.check_tls(ROW["hostname"],ctx)
        self.assertEqual(dial.call_args.args[0],("https",443))
        self.assertEqual(ctx.wrap_socket.call_args.kwargs, {"server_hostname":ROW["hostname"]})


if __name__ == "__main__":
    unittest.main()
