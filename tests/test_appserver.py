from pathlib import Path
import sys
import tempfile
import unittest

from ub_execute.appserver import AppServer, NeedsInput
from ub_execute.store import WorkflowError

FAKE_SERVER = r'''
import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    name=request['method']
    result={}
    if name=='turn/start': result={'turn':{'id':'turn-test'}}
    if name=='needs_input':
        print(json.dumps({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{}}),flush=True)
        continue
    if name=='failure':
        print(json.dumps({'id':request['id'],'error':{'code':-1,'message':'An operation failed.'}}),flush=True)
        continue
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
    if name=='turn/start':
        print(json.dumps({'method':'turn/started','params':{'turn':{'id':'turn-test'}}}),flush=True)
        print(json.dumps({'method':'turn/completed','params':{'turn':{'id':'turn-test','status':'completed'}}}),flush=True)
'''


class AppServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        script = root / "fake_server.py"
        script.write_text(FAKE_SERVER)
        self.client = AppServer(root, root / "server.log", [sys.executable, str(script)])

    def test_real_transport_initializes_streams_and_closes(self):
        with self.client as client:
            result = client.request("turn/start", {})
            self.assertEqual(client.wait_turn(result["turn"]["id"], 3)["status"], "completed")
        self.assertIsNotNone(self.client.process.returncode)

    def test_server_errors_and_input_requests_surface(self):
        with self.client as client:
            with self.assertRaises(WorkflowError):
                client.request("failure", {})
            with self.assertRaises(NeedsInput):
                client.request("needs_input", {})

    def test_waiting_for_an_unfinished_turn_has_a_deadline(self):
        with self.client as client:
            with self.assertRaisesRegex(WorkflowError, "timed out"):
                client.wait_turn("missing-turn", 0.02)


if __name__ == "__main__":
    unittest.main()
