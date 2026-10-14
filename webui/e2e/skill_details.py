"""Inspect an existing workspace Skill card in the real app; never chat or delete."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-id', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--user-id', default='dev')
    parser.add_argument('--skill', default='financial-analysis')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--web-url', default='http://127.0.0.1:8080')
    parser.add_argument('--output-dir', type=Path, default=Path('/e2e-output'))
    args = parser.parse_args()
    output = args.output_dir / ('skill-details-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    result = {'status': 'running', 'checks': [], 'page_errors': [], 'blocked_writes': [], 'model_calls': 0}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(locale='zh-CN', viewport={'width': 1440, 'height': 1000})
        storage = json.dumps({'server_url': args.api_url, 'username': args.user_id, 'i18nextLng': 'zh'})
        context.add_init_script(f'for (const [k,v] of Object.entries({storage})) localStorage.setItem(k,v);')
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error).split('\n')[0]))
        def guard(route):
            path = urlsplit(route.request.url).path
            if route.request.method == 'DELETE' or (route.request.method == 'POST' and path.startswith('/chat')):
                result['blocked_writes'].append({'method': route.request.method, 'path': path})
                route.abort()
            else:
                route.continue_()
        page.route(args.api_url + '/**', guard)
        opener = page.get_by_role('button', name=args.skill, exact=True)
        drawer = page.locator('[data-slot="drawer-content"]')
        def ensure_panel():
            page.locator('textarea:visible').last.wait_for()
            page.locator('button:has(svg.lucide-panel-right)').click()
            toggle = page.get_by_role('menuitemcheckbox', name='技能', exact=True)
            if toggle.get_attribute('aria-checked') != 'true':
                toggle.click()
            page.keyboard.press('Escape')
            expect(opener).to_be_visible()
        try:
            response = page.request.get(args.api_url + '/workspace/skill',
                params={'agent_id': args.agent_id, 'session_id': args.session_id}, headers={'X-User-ID': args.user_id})
            assert response.ok, response.status
            skill = next(item for item in response.json() if item['name'] == args.skill)
            headings = re.findall(r'^#{1,6}\s+(.+)$', skill['markdown'], re.M)
            assert headings, 'This test expects the financial methodology headings'
            page.goto(f'{args.web_url}/chat/{args.agent_id}/{args.session_id}', wait_until='domcontentloaded')
            ensure_panel()
            opener.click()
            expect(drawer).to_be_visible()
            for heading in headings:
                expect(drawer.get_by_role('heading', name=heading, exact=True)).to_be_visible()
            expect(drawer.locator('.cn-markdown')).not_to_contain_text('该技能没有提供')
            page.wait_for_function("() => { const n = document.querySelector('[data-slot=drawer-content]'); return n && n.getBoundingClientRect().right <= innerWidth + 1; }")
            page.screenshot(path=str(output / 'opened.png'), full_page=True)
            page.keyboard.press('Escape')
            expect(drawer).to_be_hidden()
            expect(opener).to_be_focused()
            result['checks'].append('mouse_opens_attached_markdown_escape_restores_focus')
            for key in ('Enter', 'Space'):
                opener.focus()
                opener.press(key)
                expect(drawer).to_be_visible()
                drawer.get_by_role('button', name='关闭', exact=True).click()
                expect(drawer).to_be_hidden()
                expect(opener).to_be_focused()
            result['checks'].append('keyboard_enter_space_and_explicit_close')
            opener.locator('..').hover()
            opener.locator('..').get_by_role('button', name='删除', exact=True).click()
            dialog = page.get_by_role('dialog')
            expect(dialog.get_by_role('button', name='取消', exact=True)).to_be_visible()
            expect(drawer).to_be_hidden()
            dialog.get_by_role('button', name='取消', exact=True).click()
            expect(dialog).to_be_hidden()
            result['checks'].append('delete_opens_confirmation_only_cancel_preserves_skill')
            page.reload(wait_until='domcontentloaded')
            ensure_panel()
            opener.click()
            expect(drawer.get_by_role('heading', name=headings[0], exact=True)).to_be_visible()
            page.wait_for_function("() => { const n = document.querySelector('[data-slot=drawer-content]'); return n && n.getBoundingClientRect().right <= innerWidth + 1; }")
            page.screenshot(path=str(output / 'after-reload.png'), full_page=True)
            result['checks'].append('reload_preserves_clickable_skill_body')
            assert not result['page_errors'], result['page_errors']
            assert not result['blocked_writes'], result['blocked_writes']
            result['status'] = 'passed'
        except Exception as error:
            result.update(status='failed', failure={'type': type(error).__name__, 'message': str(error)[:1800]})
            page.screenshot(path=str(output / 'failure.png'), full_page=True)
        finally:
            (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': result['status'], 'report': str(output / 'report.json')}, ensure_ascii=False))
            browser.close()
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
