import tempfile
import unittest
from pathlib import Path
from itsar.config import Settings
from itsar.db import open_db
from itsar.repo import Repo
from itsar.evidence import StateLock, _forbidden
from itsar.healing import _ensure_patch_shape
from itsar.tools.mock import MockDut, MockCliAdapter, MockBrowserAdapter
from itsar.tools.registry import ToolRegistry
from itsar.orchestrator import Pipeline

ROOT = Path(__file__).resolve().parents[1]
class Smoke(unittest.TestCase):
    def test_mock_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings=Settings(db_url='sqlite::memory:',out_dir=root/'out',evidence_dir=root/'evidence')
            repo=Repo(open_db(settings.db_url)); dut=MockDut(); registry=ToolRegistry()
            registry.register(MockCliAdapter(dut));registry.register(MockBrowserAdapter(dut))
            pipe=Pipeline(settings,repo,registry)
            spec={'family':{'vendor':'Synthetic','name':'Router'},'model':'MockRouter','firmware':'4.2.1','host':'192.0.2.1','credential_ref':'placeholder'}
            ctx=pipe.ingest(ROOT/'demo/placeholder_report.json',spec)
            pipe.run_phase1(ctx);pipe.run_phase2(ctx); result=pipe.run_phase3(ctx)
            self.assertEqual(result[0]['result'],'pass')
            self.assertEqual(len(result[0]['evidence']),1)
            content=repo.db.query_one('SELECT content FROM script')['content']
            compile(content, '<generated script>', 'exec')
            report=pipe.run_phase5(ctx); self.assertTrue(Path(report['html']).exists())
            with self.assertRaises(Exception): repo.db.execute('DELETE FROM plan')
    def test_locks_and_rejection(self):
        self.assertFalse(StateLock('net_idle').verify('anything')[0])
        self.assertIsNone(_forbidden('\nSSH protocol version: 2.0\n'))
        self.assertEqual(_forbidden('404 not found'), 'HTTP_ERROR')
        self.assertEqual(_ensure_patch_shape({'kind':'bad'})['kind'],'abort')
    def test_mock_state_isolation(self):
        a,b=MockDut(),MockDut(); a._pending['x']=1
        self.assertNotIn('x',b._pending)
if __name__ == '__main__': unittest.main()
