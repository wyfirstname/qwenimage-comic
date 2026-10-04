r"""端到端验证：漫画成品导出为整部 PDF（mock 引擎真实落盘）。

运行：backend> ..\.venv\Scripts\python.exe -u tests\test_pdf_export.py
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='pdf_export_test_'))
os.environ['ENGINE_MODE'] = 'mock'
os.environ['DB_PATH'] = str(TMP / 't.db')
os.environ['DATA_DIR'] = str(TMP / 'data')
os.environ['OUTPUT_DIR'] = str(TMP / 'data' / 'outputs')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as appdb  # noqa: E402
appdb.db  # noqa: E402  触发模块级单例（首次 read/write 自动建库并跑迁移）

from app import comic_repo as crepo  # noqa: E402
from app.inference import scheduler  # noqa: E402
from app.services import comic as cs  # noqa: E402
from app.services import generation  # noqa: E402

OUT = Path(os.environ['OUTPUT_DIR'])
fails = []

SCRIPT = """第 1 页
[全景] 天台：雨夜的城市天台，霓虹倒映在积水上
林晚：「你到底还是来了。」
[特写] 林晚：雨水顺着脸颊滑落
[音效：轰隆] 闪电撕裂夜空
[近景] 陈墨：他缓缓抬起手
[中景] 天台：两个人隔着雨幕对望
[远景] 城市：雨幕里的高楼只剩剪影
[中景] 天台：雨水顺着排水口汇成细流
"""

EXPECT_PANELS = 6      # 6 格显式景别 → grid_2x2（4/页）正好 2 页
EXPECT_PAGES = 2


def check(name, ok, extra=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'  {extra}' if extra else ''), flush=True)
    if not ok:
        fails.append(name)


async def wait_panels(project_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        panels = crepo.list_panels(project_id)
        if panels and not any(p['status'] in ('queued', 'running') for p in panels):
            return panels
        await asyncio.sleep(0.2)
    return crepo.list_panels(project_id)


def pdf_page_count(path: Path) -> int:
    data = path.read_bytes()
    # PIL 写出的页对象形如 << /Type /Page ... >>；/Pages 是目录节点，不计入
    return len(re.findall(rb'/Type\s*/Page[^s]', data))


async def main():
    consumer = asyncio.create_task(scheduler.consume_loop(generation.run_job))

    print('=== 1. 准备项目：角色 + 场景 + 5 格分镜并出图 ===', flush=True)
    pid = crepo.create_project(title='雨夜天台', style='jp_color', layout='grid_2x2',
                               width=256, height=256, steps=1, guidance=4.0)['id']
    project = crepo.get_project(pid)
    crepo.create_character(pid, name='林晚', gender='女', age='20 岁',
                           appearance='黑长直', outfit='风衣')
    crepo.create_character(pid, name='陈墨', gender='男', age='25 岁',
                           appearance='短发', outfit='夹克')
    crepo.create_scene(pid, name='天台', location='楼顶', time_of_day='夜')
    cs.create_panels_from_script(pid, SCRIPT, replace=True)
    panels = crepo.list_panels(pid)
    check(f'剧本解析出 {EXPECT_PANELS} 格', len(panels) == EXPECT_PANELS, str(len(panels)))
    await cs.enqueue_project(pid, only_missing=True)
    panels = await wait_panels(pid)
    done = [p for p in panels if p.get('image_url')]
    check('全部分镜出图完成', len(done) == EXPECT_PANELS, f'{len(done)}/{EXPECT_PANELS}')

    print(f'=== 2. 生成漫画页（grid_2x2 → {EXPECT_PAGES} 页）===', flush=True)
    res = cs.render_project_pages(pid)
    check(f'排版生成 {EXPECT_PAGES} 页', res['pages'] == EXPECT_PAGES, f"pages={res['pages']}")
    renders = crepo.list_renders(pid, limit=100)
    check('成品页已入库', len(renders) == EXPECT_PAGES, str(len(renders)))

    print('=== 3. 导出整部 PDF ===', flush=True)
    exp = cs.export_project_pdf(pid)
    pdf = Path(exp['path'])
    check('PDF 文件已生成', pdf.is_file(), str(pdf))
    check('PDF 落在项目目录里', pdf.parent == cs.project_output_dir(project),
          str(pdf.parent))
    check('文件名后缀 .pdf', pdf.name.endswith('.pdf'), pdf.name)
    from app.config import settings as app_settings

    if pdf.resolve().is_relative_to(app_settings.project_root.resolve()):
        check('URL 指向静态目录', exp['url'].startswith('/data/outputs/comics/'), exp['url'])
    else:
        # 测试把输出目录指到了临时目录（项目外），relative_url 会回退成绝对路径；
        # 真实部署里 outputs 在项目根下，拿到的是 /data/outputs/... 静态 URL。
        check('目录在项目外 → URL 回退为绝对路径', Path(exp['url']).is_absolute(), exp['url'])
    data = pdf.read_bytes()
    check('是合法 PDF 头', data[:5] == b'%PDF-', str(data[:8]))
    check('落盘后仍有尾部标记', b'%%EOF' in data[-2048:])
    check('PDF 页数与成品页一致', pdf_page_count(pdf) == EXPECT_PAGES, f"count={pdf_page_count(pdf)}")
    check('返回 pages 字段正确', exp['pages'] == EXPECT_PAGES, str(exp['pages']))
    check('返回体积 > 0', exp['size'] > 0, str(exp['size']))

    print('=== 4. 导出记录可被列出 ===', flush=True)
    exports = cs.project_exports(project)
    check('列表含刚导出的 PDF', any(x['name'] == pdf.name for x in exports),
          str([x['name'] for x in exports]))
    detail = cs.project_detail(pid)
    check('项目详情带 exports 字段', any(x['name'] == pdf.name for x in detail['exports']))

    print('=== 5. 重复导出不覆盖，生成新文件 ===', flush=True)
    time.sleep(1.1)   # 时间戳精确到秒，稍等以免同名
    exp2 = cs.export_project_pdf(pid)
    check('第二次导出文件不同', exp2['path'] != exp['path'], f"{pdf.name} vs {exp2['filename']}")
    check('两个 PDF 都在列表里', len(cs.project_exports(project)) == 2)

    print('=== 6. 无成品页时「一键导出」自动先排版 ===', flush=True)
    for rnd in crepo.list_renders(pid, limit=100):
        crepo.delete_render(rnd['id'])
    check('成品页已清空', not crepo.list_renders(pid, limit=100))
    exp3 = cs.export_project_pdf(pid)
    check('自动排版后又有了成品页', len(crepo.list_renders(pid, limit=100)) == EXPECT_PAGES)
    check('自动排版后 PDF 页数正确', exp3['pages'] == EXPECT_PAGES, str(exp3['pages']))

    print('=== 7. rerender=True 强制重排（历史批次不应重复进 PDF）===', flush=True)
    time.sleep(1.1)
    exp4 = cs.export_project_pdf(pid, rerender=True)
    all_renders = crepo.list_renders(pid, limit=1000)
    check('重排后成品页累计 > 页数（有历史批次）', len(all_renders) > EXPECT_PAGES,
          f'{len(all_renders)} 份')
    check('PDF 每个页码只取最新一份', exp4['pages'] == EXPECT_PAGES,
          f"pages={exp4['pages']}, 库内={len(all_renders)}")
    check('PDF 页数仍与成品页一致', pdf_page_count(Path(exp4['path'])) == EXPECT_PAGES,
          f"count={pdf_page_count(Path(exp4['path']))}")

    print('=== 8. 删除导出 PDF（含安全校验）===', flush=True)
    cs.delete_project_export(pid, exp4['filename'])
    check('指定 PDF 已删除', not Path(exp4['path']).exists())
    for evil in ('../../app.db', 'a\\b.pdf', 'x.txt', '..', ''):
        try:
            cs.delete_project_export(pid, evil)
            check(f'非法/越界名被拦下: {evil!r}', False, '未报错')
        except cs.ComicError as exc:
            check(f'非法/越界名被拦下: {evil!r}', True, str(exc)[:40])
    check('项目外的文件安然无恙', (TMP / 't.db').exists())

    print('=== 9. 没有分镜图时报错清晰 ===', flush=True)
    pid2 = crepo.create_project(title='空项目', width=256, height=256, steps=1)['id']
    try:
        cs.export_project_pdf(pid2)
        check('空项目导出应报错', False, '未报错')
    except cs.ComicError as exc:
        check('空项目导出报错清晰', '分镜' in str(exc), str(exc))

    consumer.cancel()

    print('=== 项目目录内容 ===', flush=True)
    for p in sorted(cs.project_output_dir(project).iterdir()):
        print(f'   {p.name}  ({p.stat().st_size} B)', flush=True)


asyncio.run(main())
print()
print('结果:', 'ALL PASS' if not fails else f'{len(fails)} 项失败: {fails}')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
