"""Independent red regressions for 0c512b4. No production data or org access."""
import http.client
import importlib.util
import io
import json
import secrets
import sys
import threading
import zipfile
from pathlib import Path

import pytest

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from mct import baseline, config, migration, comparison
from mct.retrieved_folder_compare import compare_trees
from mct.ui_server_base import UIHTTPServer
spec=importlib.util.spec_from_file_location('independent_diff_ui',REPO/'scripts/serve-diff-ui.py')
ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('MCT_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('MCT_CONFIG_DIR',str(tmp_path/'config'))
    monkeypatch.setattr(ui,'BASELINE_FILE',None)
    monkeypatch.setattr(ui,'PAIR_KEY',None)
    ui.get_comparison.cache_clear()
    yield
    ui.get_comparison.cache_clear()

def put(root,name,text):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p

def handler(left,right):
    return object.__new__(ui.make_handler(left,right,'left','right'))

def test_numbered_criteria_reordering_is_real_drift(tmp_path):
    def xml(order):
        items=''.join(f'<criteriaItems><field>Account.{x}__c</field><operation>equals</operation><value>true</value></criteriaItems>' for x in order)
        return '<Workflow xmlns="http://soap.sforce.com/2006/04/metadata"><rules><fullName>Review</fullName><active>true</active><booleanFilter>1 AND (2 OR 3)</booleanFilter>'+items+'<triggerType>onCreateOnly</triggerType></rules></Workflow>'
    left,right=tmp_path/'left',tmp_path/'right'
    put(left,'workflows/Account.workflow-meta.xml',xml(['A','B','C']))
    put(right,'workflows/Account.workflow-meta.xml',xml(['B','A','C']))
    result=compare_trees(left,right)
    assert len(result.differ_pairs)==1,'numbered Workflow criteria change was hidden as identical'

def test_static_resource_zip_contains_entire_component(tmp_path):
    left,right=tmp_path/'left',tmp_path/'right'
    for side,value in [(left,'1'),(right,'2')]:
        put(side,'staticresources/ReviewAsset/app.js','window.value='+value+';')
        put(side,'staticresources/ReviewAsset/unchanged.txt','required asset')
        put(side,'staticresources/ReviewAsset.resource-meta.xml','<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata"><cacheControl>Public</cacheControl><contentType>application/zip</contentType></StaticResource>')
    h=handler(left,right);h._read_body=lambda:{}
    h._resolve_components_via_sf=lambda paths:({'StaticResource':{'ReviewAsset'}} if paths else {},None)
    captured={};h._serve_bytes=lambda body,status=200,headers=None:captured.update(body=body,status=status)
    h._serve_json=lambda body,status=200:captured.update(error=body,status=status)
    h._handle_export_bundle()
    assert captured['status']==200,captured.get('error')
    with zipfile.ZipFile(io.BytesIO(captured['body'])) as z:
        required={'delta-source/staticresources/ReviewAsset/app.js','delta-source/staticresources/ReviewAsset/unchanged.txt','delta-source/staticresources/ReviewAsset.resource-meta.xml'}
        assert required.issubset(z.namelist()),'successful ZIP omitted required StaticResource files'

def test_interrupted_migration_can_be_retried(tmp_path,monkeypatch):
    src=tmp_path/'legacy/project';put(src,'snapshots.json','{"snapshots":[{"id":"s1","path":"snap/f.txt"}]}');put(src,'snap/f.txt','original')
    real_copy=migration.shutil.copytree
    def partial(source,target,*args,**kwargs):
        put(Path(target),'snapshots.json',(src/'snapshots.json').read_text())
        raise OSError('interrupted test copy')
    monkeypatch.setattr(migration.shutil,'copytree',partial)
    assert migration.migrate(tmp_path/'legacy',tmp_path/'data')['errors']
    monkeypatch.setattr(migration.shutil,'copytree',real_copy)
    again=migration.migrate(tmp_path/'legacy',tmp_path/'data')
    assert again['migrated']==['project'],'partial final directory blocks retry as an ordinary conflict'
    assert (tmp_path/'data/snapshot-store/project/snap/f.txt').read_text()=='original'

def test_empty_selection_never_exports_deletions(tmp_path):
    left,right=tmp_path/'left',tmp_path/'right';left.mkdir()
    put(right,'classes/TargetOnly.cls','public class TargetOnly {}')
    assert handler(left,right)._collect_delta_files([])==([],[]),'empty selection expanded to all active drift'

def test_accepted_diff_absent_from_active_html_section(tmp_path,monkeypatch):
    left,right=tmp_path/'left',tmp_path/'right'
    lp=put(left,'classes/Known.cls','public class Known {Integer v=1;}')
    rp=put(right,'classes/Known.cls','public class Known {Integer v=2;}')
    bl=tmp_path/'baseline.json';monkeypatch.setattr(ui,'BASELINE_FILE',bl)
    baseline.accept_diff('classes/Known.cls',lp,rp,baseline_file=bl)
    assert ui.build_summary(left,right,'left','right')['different_count']==0
    html=ui._build_standalone_report(handler(left,right))
    active=html.split('<h2>Changed files (active)</h2>',1)[1].split('<h2>Only in left',1)[0]
    assert 'classes/Known.cls' not in active,'accepted difference mislabeled as active'

def test_workspace_migration_switches_to_new_copy(tmp_path,monkeypatch):
    monkeypatch.delenv('MCT_CONFIG_DIR')
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(config,'config_root',lambda:tmp_path/'new-config')
    legacy=put(tmp_path,'.config/mct/workspaces.json','{"workspaces":[]}')
    assert migration.migrate_workspaces()['migrated']
    assert config.workspaces_path()==tmp_path/'new-config/workspaces.json','successful migration still routes writes to legacy'
    assert legacy.exists()

def test_malformed_matrix_remains_json_and_exits_two(capsys):
    code=comparison.run_compare_matrix(['not-a-pair'],True,True)
    output=capsys.readouterr()
    assert code==2,'malformed pair reported as drift exit 1 instead of error exit 2'
    assert json.loads(output.out)['has_errors'] is True

def test_head_cannot_bypass_guard(tmp_path,monkeypatch):
    put(tmp_path,'pyproject.toml','[project]\nname = \"synthetic-review-fixture\"\n')
    monkeypatch.chdir(tmp_path)
    server=UIHTTPServer(('127.0.0.1',0),ui.DiffUIHandler);server.session_token=secrets.token_urlsafe(32)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        conn.request('HEAD','/pyproject.toml',headers={'Host':'untrusted.example','Origin':'https://untrusted.example'})
        response=conn.getresponse();response.read();status=response.status;conn.close()
        assert status==403,'HEAD reached inherited filesystem route without Host/Origin checks'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
