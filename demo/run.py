"""Synthetic example; never a real compliance assessment."""
from pathlib import Path
import json, sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from itsar.config import Settings
from itsar.db import open_db
from itsar.repo import Repo
from itsar.tools.mock import MockDut, MockCliAdapter, MockBrowserAdapter, OLD_FIRMWARE
from itsar.tools.registry import ToolRegistry
from itsar.orchestrator import Pipeline

def main():
    output = ROOT / 'demo' / 'results'
    output.mkdir(exist_ok=True)
    # Fresh database per demonstration so rerunning does not overwrite DB history.
    import tempfile
    run = Path(tempfile.mkdtemp(prefix='run-', dir=output))
    settings = Settings(db_url='sqlite:' + str(run / 'demo.db'), evidence_dir=run / 'evidence', out_dir=run / 'out')
    repo = Repo(open_db(settings.db_url))
    dut = MockDut(profile=OLD_FIRMWARE)
    registry = ToolRegistry(); registry.register(MockCliAdapter(dut)); registry.register(MockBrowserAdapter(dut))
    pipeline = Pipeline(settings, repo, registry)
    spec = {'family': {'vendor':'Synthetic', 'name':'Router'}, 'model':'MockRouter', 'firmware':'4.2.1', 'host':'192.0.2.1', 'credential_ref':'placeholder'}
    ctx = pipeline.ingest(ROOT / 'demo' / 'placeholder_report.json', spec)
    one = pipeline.run_phase1(ctx); two = pipeline.run_phase2(ctx); three = pipeline.run_phase3(ctx); report = pipeline.run_phase5(ctx)
    print(json.dumps({'run_directory':str(run), 'phase1':one['state'], 'phase2_plans':len(two), 'phase3_results':[x['result'] for x in three], 'report':report}, indent=2))

if __name__ == '__main__': main()
