"""Independent remediation follow-up: synthetic fixtures, no org access."""
import importlib.util,sys,json,io,zipfile
from pathlib import Path
from urllib.parse import unquote
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from mct import baseline,config,validate
from mct.retrieved_folder_compare import LinkedMetadataError
spec=importlib.util.spec_from_file_location('independent_ui',ROOT/'scripts/serve-diff-ui.py')
ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)

@pytest.fixture
def pair(tmp_path,monkeypatch):
    ui.get_comparison.cache_clear()
    monkeypatch.setattr(ui,'BASELINE_FILE',None)
    monkeypatch.setattr(ui,'INCLUDE_TYPES',None)
    monkeypatch.setattr(ui,'EXCLUDE_TYPES',None)
    for side,text in [('left','local'),('right','remote')]:
        p=tmp_path/side/'classes';p.mkdir(parents=True);(p/'A.cls').write_text(text)
    return tmp_path/'left',tmp_path/'right'

def handler(pair):
    h=object.__new__(ui.DiffUIHandler)
    h.left_root,h.right_root=pair;h.left_rel='left';h.right_rel='right';h.left_info=h.right_info=None;h.api_version='66.0'
    out={};h._serve_json=lambda data,status=200:out.update(status=status,json=data)
    h._serve_bytes=lambda data,status=200,headers=None:out.update(status=status,bytes=data)
    h._read_body=lambda:{}
    return h,out

def swap_parent(pair,tmp_path):
    ui.get_comparison(*pair)
    external=tmp_path/'external';external.mkdir();(external/'A.cls').write_text('EXTERNAL_PRIVATE_MARKER')
    (pair[0]/'classes').rename(pair[0]/'classes.original')
    (pair[0]/'classes').symlink_to(external,target_is_directory=True)

@pytest.mark.parametrize('route',['diff','report','bundle'])
def test_cached_parent_link_never_exposes_external_content(pair,tmp_path,route):
    h,out=handler(pair);swap_parent(pair,tmp_path)
    if route=='diff':
        h.path='/api/diff?path=classes/A.cls';h._route_GET()
        exposed='EXTERNAL_PRIVATE_MARKER' in json.dumps(out)
    elif route=='report':
        h._handle_export_html();exposed='EXTERNAL_PRIVATE_MARKER' in unquote(json.dumps(out))
    else:
        h._resolve_components_via_sf=lambda paths:({'ApexClass':{'A'}},None)
        h._handle_export_bundle()
        exposed=False
        if 'bytes' in out:
            with zipfile.ZipFile(io.BytesIO(out['bytes'])) as z:
                exposed=any(b'EXTERNAL_PRIVATE_MARKER' in z.read(n) for n in z.namelist())
    assert not exposed,f'{route} leaked synthetic external content after a directory became a symlink'

def test_fingerprint_refuses_direct_link(tmp_path):
    marker=tmp_path/'outside';marker.write_text('EXTERNAL_PRIVATE_MARKER')
    link=tmp_path/'fake.cls';link.symlink_to(marker)
    with pytest.raises((baseline.FingerprintReadError,LinkedMetadataError,RuntimeError)):
        baseline.compute_fingerprint(link,None)

def test_filtered_summary_counts_match_scope(pair,monkeypatch):
    for root in pair:
        p=root/'reports';p.mkdir();(p/'R.report').write_text('unchanged report')
    monkeypatch.setattr(ui,'INCLUDE_TYPES',frozenset({'classes'}))
    summary=ui.build_summary(*pair,'left','right')
    assert summary['different_count']==1
    assert summary['identical_count']==0,summary
    assert summary['total_left']==summary['total_right']==1,summary

def test_default_cache_call_respects_changed_scope(pair,monkeypatch):
    for root in pair:
        p=root/'reports';p.mkdir();(p/'R.report').write_text(str(root))
    monkeypatch.setattr(ui,'INCLUDE_TYPES',frozenset({'classes'}))
    assert len(ui.build_summary(*pair,'left','right')['differ'])==1
    monkeypatch.setattr(ui,'INCLUDE_TYPES',frozenset({'reports'}))
    result=ui.build_summary(*pair,'left','right')
    assert [e['path'] for e in result['differ']]==['reports/R.report'],result

def test_original_binary_fingerprint_and_report_fixed(pair,tmp_path):
    l=pair[0]/'classes/A.resource';r=pair[1]/'classes/A.resource'
    l.write_bytes(b'\x00\x80');r.write_bytes(b'\x00\x82')
    bfile=tmp_path/'baseline.json';baseline.accept_diff('classes/A.resource',l,r,baseline_file=bfile)
    r.write_bytes(b'\x00\x83')
    assert baseline.classify_entry('classes/A.resource',l,r,baseline.load_baseline(bfile))[0]=='accepted_stale'
    h,out=handler(pair);h._handle_export_html()
    assert 'classes/A.resource' in out['json']['html']
    assert 'Binary content changed' in out['json']['html']

def test_original_deletion_only_is_refused(tmp_path,monkeypatch):
    monkeypatch.setenv('MCT_DATA_DIR',str(tmp_path/'data'))
    (tmp_path/'left').mkdir();(tmp_path/'right/classes').mkdir(parents=True)
    (tmp_path/'right/classes/DeleteMe.cls').write_text('class DeleteMe {}')
    with config.repo_context(str(tmp_path)):
        assert validate.run_validate_deploy('left','right','not-contacted','66.0',60)==1
        reports=list(config.STORAGE_ROOT.glob('validation-report-*.json'))
        assert json.loads(reports[0].read_text())['error']=='unsupported_delta_scope'

def test_original_missing_empty_are_distinct(tmp_path):
    p=tmp_path/'empty';p.write_bytes(b'')
    other=tmp_path/'other';other.write_bytes(b'content')
    assert baseline.compute_fingerprint(p,other)!=baseline.compute_fingerprint(None,other)

def test_original_initial_link_is_blocked(pair,tmp_path):
    marker=tmp_path/'outside';marker.write_text('synthetic marker')
    (pair[0]/'classes/External.cls').symlink_to(marker)
    with pytest.raises(LinkedMetadataError): ui.get_comparison(*pair)

def test_original_warnings_are_in_html_report(pair):
    h,out=handler(pair)
    h.right_info={'type':'org_retrieve','manifest_kind':'union-manifest','skipped_org_types':['Report'],'retrieve_warnings':['MISSING_SAMPLE_COMPONENT']}
    h._handle_export_html()
    assert out['status']==200
    html=out['json']['html']
    assert 'MISSING_SAMPLE_COMPONENT' in html
    assert 'skipped org-only type(s)' in html
