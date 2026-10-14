"""Real React/Streamdown browser regression with mocked finance APIs/native events.

Serve frontend/tests/report-harness.html through Vite on 18180. This test never
sends chat, provisions credentials, or accesses an external financial/model API.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from acceptance import Acceptance

BASE = os.getenv('FIN_FIXTURE_URL', 'http://127.0.0.1:18180')
OUT = Path(os.getenv('FIN_E2E_OUTPUT', '/e2e-output')) / 'report-fixture'
ORIGINAL = 'Original filing documents were not fetched; source filing links are metadata only.'
QUARTERS = 'Cross-company quarters retain actual fiscal dates and can cover different calendar windows.'
PERIODS = 'Cross-company periods retain actual fiscal dates; equal row positions do not imply calendar alignment.'
CONTACT = 'SEC_USER_AGENT is using FinanceAgent/0.1; configure an honest application name and real contact email for SEC fair-access identification.'
CELL = {'cell_id':'c_fixture','metric':'revenue','label':'Revenue','value':'100','unit':'USD','status':'available','source_ids':['s_fixture'],'inputs':[],'formula':None,'reason':None}
ROW = {'company':'AAPL','period_start':'2026-01-01','period_end':'2026-03-31','period_label':'Fixture quarter','cells':[CELL]}
SOURCE = {'source_id':'s_fixture','fact_id':1,'company':'AAPL','concept':'us-gaap:Revenue','metric':'revenue','period_start':'2026-01-01','period_end':'2026-03-31','value':'100','unit':'USD','accession':'fixture','form':'10-Q','filed_at':'2026-04-30','retrieved_at':'2026-10-14T00:00:00Z','api_url':'https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json','filing_url':'https://www.sec.gov/Archives/','document_status':'metadata_only','note':None}
DATA = {'artifact_id':'dataset_fixture','run_id':'run_fixture','kind':'dataset','title':'Fixture data','created_at':'2026-10-14T00:00:00Z','input_ids':[],'data':{'rows':[ROW],'sources':{'s_fixture':SOURCE},'history_rows':[],'specification':{'requested_metrics':['revenue']},'warnings':[ORIGINAL,QUARTERS,CONTACT],'selection_policy':'fixture'},'text':None,'citations':[],'warnings':[ORIGINAL,QUARTERS,CONTACT],'filename':None,'mime_type':None}
BODY = '第一段：营收为100。[证据](#finance/dataset_fixture/c_fixture)\n\n第二段：这是完整报告。'
REPORT = {**DATA,'artifact_id':'narrative_fixture','kind':'narrative','title':'AAPL 财务分析','input_ids':['dataset_fixture'],'data':None,'text':BODY,'citations':[{'artifact_id':'dataset_fixture','cell_id':'c_fixture','label':'证据'}],'warnings':[ORIGINAL,QUARTERS,PERIODS,CONTACT]}

HTML = b'<html><body>filing-only-marker<script>window.__rawFilingExecuted=true</script></body></html>'
DOCUMENT_INFO = {'source_document_id':'sec_document_fixture','company':'AAPL','cik':'0000320193','form':'10-Q','period_end':'2026-03-31','filed_at':'2026-04-30','accession':'0000320193-26-000001','primary_document':'aapl-fixture.htm','source_url':'https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-fixture.htm','sha256':hashlib.sha256(HTML).hexdigest(),'byte_size':len(HTML),'media_type':'text/html','fetched_at':'2026-10-14T00:00:00Z'}
DOCUMENT = {**DATA,'artifact_id':'document_fixture','kind':'document','title':'AAPL original filing','data':None,'document':DOCUMENT_INFO,'warnings':[],'filename':'aapl-fixture.htm','mime_type':'application/octet-stream'}
DOCUMENT_BODY = '这段分析引用原始文件。[财报原文](#filing/document_fixture)'
DOCUMENT_REPORT = {**REPORT,'artifact_id':'narrative_document_fixture','input_ids':['document_fixture'],'text':DOCUMENT_BODY,'citations':[{'document_id':'document_fixture','label':'财报原文'}],'warnings':[]}


def frame(kind, **payload):
    return json.dumps({'finance_stream':1,'type':kind,**payload}, ensure_ascii=False) + '\n'


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    result = {'status':'running','checks':[], 'page_errors':[], 'model_calls':0}
    artifacts = [DATA, DOCUMENT]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(locale='zh-CN', viewport={'width':1440,'height':1000})
        context.add_init_script("localStorage.setItem('server_url','http://fixture.invalid');localStorage.setItem('username','fixture');localStorage.setItem('i18nextLng','zh');")
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error).split('\n')[0]))
        def api(route):
            path = urlsplit(route.request.url).path
            headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, OPTIONS'}
            if route.request.method == 'OPTIONS':
                route.fulfill(status=204, headers=headers)
            elif path == '/fin/results':
                route.fulfill(json={'artifacts':artifacts}, headers=headers)
            elif path == '/fin/results/dataset_fixture/cells/c_fixture':
                route.fulfill(json={'artifact_id':'dataset_fixture','title':'Fixture data','row':ROW,'cell':CELL,'sources':[SOURCE],'warnings':[ORIGINAL]}, headers=headers)
            elif path == '/fin/results/document_fixture/document':
                route.fulfill(json={'artifact_id':'document_fixture','title':DOCUMENT['title'],'document':DOCUMENT_INFO,'warnings':[]}, headers=headers)
            elif path == '/fin/artifacts/document_fixture/download':
                route.fulfill(body=HTML, content_type='application/octet-stream', headers={**headers,'Content-Disposition':'attachment; filename="aapl-fixture.htm"','X-Content-Type-Options':'nosniff'})
            else:
                route.abort()
                raise AssertionError('Unexpected fixture API request: ' + path)
        page.route('http://fixture.invalid/**', api)
        panel = page.locator('[data-testid="finance-report"]')
        cards = page.locator('[data-finance-report-card]')
        source = page.locator('[data-testid="finance-provenance"]')
        def delta(text):
            page.evaluate('text => window.financeFixture.delta(text)', text)
        def open_report():
            cards.first.locator('[data-finance-report-open]').click()
            expect(panel).to_be_visible()
        def close_report():
            page.keyboard.press('Escape')
            expect(panel).to_be_hidden()
        try:
            page.goto(BASE + '/tests/report-harness.html', wait_until='networkidle')
            expect(cards).to_have_count(1)
            expect(cards.locator('.cn-markdown')).to_have_count(0)
            # Same real Markdown wrapper as assistant replies, with animation enabled.
            for label in ['OCF','营收','比率','Paragraph source','List source']:
                anchor = page.locator('article').get_by_role('link',name=label,exact=True)
                expect(anchor).to_have_attribute('data-finance-citation','')
                anchor.click()
                expect(source).to_be_visible()
                expect(source.locator('dl')).to_have_count(1)
                page.keyboard.press('Escape')
                expect(source).to_be_hidden()
            expect(page.get_by_text('Ordinary link',exact=True)).not_to_have_attribute('data-finance-citation','')
            result['checks'].append('real_streamdown_table_paragraph_list_citations_and_external_link_safety')
            open_report()
            panel.evaluate("node => node.dataset.fixtureIdentity = 'same-panel'")
            start = frame('start',title='AAPL 财务分析')
            delta(start[:9])
            expect(panel).to_have_attribute('data-finance-status','running')
            delta(start[9:])
            activity = frame('progress',text='Read /workspace/inputs/filing.htm')
            delta(activity[:11])
            delta(activity[11:])
            expect(panel.locator('[data-finance-report-progress]')).to_contain_text('Read /workspace/inputs/filing.htm')
            expect(cards.first).to_have_attribute('data-finance-report-progress-count','1')
            expect(panel.locator('.cn-markdown')).to_have_count(0)
            observer = Acceptance.__new__(Acceptance)
            observer.page = page
            assert any(tool['textStreaming'] for tool in observer.snapshot()['tools'])
            result['checks'].append('research_progress_separate_from_report_body')
            first = frame('body',text='第一段：营收为100。')
            for piece in [first[:13], first[13:-2], first[-2:]]:
                delta(piece)
            expect(panel.locator('.cn-markdown')).to_contain_text('营收为100')
            observer = Acceptance.__new__(Acceptance)
            observer.page = page
            snapshot = observer.snapshot()
            assert any(tool['textStreaming'] for tool in snapshot['tools']), json.dumps(snapshot)
            delta(frame('body',text=BODY.removeprefix('第一段：营收为100。')))
            expect(panel.locator('.cn-markdown')).to_contain_text('这是完整报告')
            panel.get_by_role('link',name='证据',exact=True).click()
            expect(source).to_be_visible()
            expect(source.locator('dl')).to_have_count(1)
            page.keyboard.press('Escape')
            expect(source).to_be_hidden()
            expect(panel).to_be_visible()
            expect(panel.get_by_role('link',name='证据',exact=True)).to_be_focused()
            result['checks'].append('first_response_card_live_native_deltas_nested_provenance_focus')
            delta(frame('summary',result={'message':'saved'}))
            expect(panel).to_have_attribute('data-finance-status','saving')
            page.evaluate('artifact => window.financeFixture.finish(artifact)', REPORT)
            expect(panel).to_have_attribute('data-finance-status','complete')
            expect(panel).to_have_attribute('data-fixture-identity','same-panel')
            expect(cards).to_have_count(1)
            expect(cards).to_have_attribute('data-finance-artifact','narrative_fixture')
            expect(panel.locator('.cn-markdown')).to_contain_text('这是完整报告')
            assert panel.locator('.cn-markdown').inner_text().count('第一段') == 1
            expect(panel.locator('.cn-markdown')).not_to_contain_text('finance_stream')
            page.screenshot(path=str(OUT/'completed.png'))
            result['checks'].append('terminal_artifact_binding_no_duplicate_text_or_card')
            close_report()
            expect(cards.first.locator('[data-finance-report-open]')).to_be_focused()
            for state in ['error','interrupted','incomplete']:
                page.evaluate('window.financeFixture.reset()')
                delta(start + frame('body',text='尚未保存的草稿。'))
                open_report()
                page.evaluate('state => window.financeFixture.stop(state)',state)
                expect(panel).to_have_attribute('data-finance-status','failed' if state == 'error' else state)
                expect(panel.locator('.cn-markdown')).to_contain_text('尚未保存')
                expect(cards).not_to_have_attribute('data-finance-artifact','narrative_fixture')
                close_report()
            result['checks'].append('failed_interrupted_incomplete_drafts_never_saved')
            page.evaluate('window.financeFixture.reset()')
            delta(start + frame('body',text='旧会话私有草稿。'))
            open_report()
            page.evaluate('window.financeFixture.switchSession()')
            expect(panel).to_be_hidden()
            open_report()
            expect(panel).not_to_contain_text('旧会话私有草稿')
            close_report()
            result['checks'].append('session_switch_clears_selected_report')
            legacy = {**REPORT,'artifact_id':'narrative_legacy','title':'请用两三句中文分析财务变化并引用具体数值单元格，不推测原文未经支持的原因。' * 3}
            artifacts.append(legacy)
            page.reload(wait_until='networkidle')
            old_card = page.locator('[data-finance-artifact="narrative_legacy"]')
            expect(old_card).to_be_visible()
            assert len(old_card.locator('h3').inner_text()) <= 64
            expect(old_card.locator('.cn-markdown')).to_have_count(0)
            old_card.locator('[data-finance-report-open]').click()
            expect(panel).to_be_visible()
            text = panel.text_content()
            assert 'Cross-company' not in text and '跨公司' not in text
            assert text.count('未获取申报原文') <= 1
            expect(panel.get_by_role('link',name='证据',exact=True)).to_be_visible()
            result['checks'].append('legacy_hydration_short_card_scope_aware_notices')
            close_report()
            page.evaluate('window.financeFixture.reset()')
            delta(start + frame('progress',text='Read /workspace/inputs/aapl-fixture.htm') + frame('body',text=DOCUMENT_BODY))
            page.evaluate('artifact => window.financeFixture.finish(artifact)', DOCUMENT_REPORT)
            open_report()
            panel.get_by_role('link',name='财报原文',exact=True).click()
            expect(source).to_be_visible()
            details = source.locator('[data-finance-document="document_fixture"]')
            expect(details).to_contain_text(DOCUMENT_INFO['sha256'])
            expect(details).to_contain_text('2026-04-30')
            expect(page.locator('iframe')).to_have_count(0)
            assert page.evaluate('window.__rawFilingExecuted') is None
            assert 'filing-only-marker' not in page.locator('body').inner_text()
            with page.expect_download() as pending_download:
                details.get_by_role('button',name=re.compile('下载|Download filing')).click()
            downloaded = pending_download.value
            downloaded.save_as(str(OUT/'aapl-fixture.htm'))
            assert (OUT/'aapl-fixture.htm').read_bytes() == HTML
            assert page.evaluate('window.__rawFilingExecuted') is None
            page.screenshot(path=str(OUT/'document-provenance.png'))
            page.keyboard.press('Escape')
            expect(source).to_be_hidden()
            expect(panel).to_be_visible()
            expect(panel.get_by_role('link',name='财报原文',exact=True)).to_be_focused()
            close_report()
            result['checks'].append('document_citation_pinned_metadata_attachment_only_download')
            page.evaluate('artifacts => window.financeFixture.partialDocuments(artifacts)', [DOCUMENT])
            partial = page.locator('[data-finance-tool="fetch_filings"]')
            expect(partial).to_have_attribute('data-finance-status','partial')
            expect(partial.locator('[data-finance-artifact="document_fixture"]')).to_be_visible()
            expect(partial.locator('[role="alert"]')).to_have_count(0)
            result['checks'].append('partial_filing_batch_keeps_downloaded_document_visible')
            assert not result['page_errors'], result['page_errors']
            result['status'] = 'passed'
        except Exception as error:
            result['status'] = 'failed'
            result['failure'] = {'type':type(error).__name__,'message':str(error)[:1600]}
            result['body_excerpt'] = page.locator('body').inner_text()[:2000]
            page.screenshot(path=str(OUT/'failure.png'))
        finally:
            (OUT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
            browser.close()
    return 0 if result['status'] == 'passed' else 1

if __name__ == '__main__':
    raise SystemExit(run())
