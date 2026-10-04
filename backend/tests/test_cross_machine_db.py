"""回归：跨机器拷贝数据库后的成品页导出（mock，不依赖真实引擎）。

场景：用户在 A 机（如 D:\\qwenimage\\portable）生成成品页，把 data 目录整体拷到 B 机。
comic_renders 里的 path 指向 A 机盘符（B 机不存在），文件本体在 B 机 outputs 下
（老式 `comics/<project_id>/` 或项目目录里）。此前 `_resolve_stored_path(path) or
_resolve_stored_path(url)` 因坏路径解析结果是"真值"而短路，导出报"成品页文件缺失"。

运行：backend> python -u tests/test_cross_machine_db.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='xmachine_test_'))
os.environ['ENGINE_MODE'] = 'mock'
os.environ['DB_PATH'] = str(TMP / 't.db')
os.environ['DATA_DIR'] = str(TMP / 'data')
os.environ['OUTPUT_DIR'] = str(TMP / 'data' / 'outputs')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as appdb  # noqa: E402
appdb.db  # noqa: E402

from PIL import Image  # noqa: E402

from app import comic_repo as crepo  # noqa: E402
from app.models_util import relative_url  # noqa: E402
from app.services import comic as cs  # noqa: E402

OUT = Path(os.environ['OUTPUT_DIR'])
GHOST = Path('Q:/qwenimage-portable/data/outputs')   # 模拟 A 机的盘符（本机不存在）
fails = []


def check(name, ok, extra=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'  {extra}' if extra else ''), flush=True)
    if not ok:
        fails.append(name)


def fake_render_png(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    Image.new('RGB', (120, 160), (120, 40, 40)).save(p)
    return p


def main():
    print('=== 1. 造数据：文件在老式 id 目录，记录指向不存在的 Q: 盘 ===', flush=True)
    pid = crepo.create_project(title='跨机迁移测试', style='ink', layout='grid_2x2',
                               width=768, height=768, steps=4, guidance=4.0)['id']
    project = crepo.get_project(pid)
    target = cs.project_output_dir(project)                 # 项目目录（迁移目标）
    old_dir = OUT / 'comics' / pid                          # 老式目录（A 机拷来的文件落在这）
    files = {}
    for page in (1, 2):
        f = fake_render_png(old_dir, f'p{page}.png')
        files[page] = f
        crepo.create_render(pid, path=str(GHOST / pid / f.name), url=relative_url(f),
                            layout='grid_2x2', page=page, width=120, height=160)
    check('Q 盘路径本机不可达', not Path(str(GHOST / pid / 'p1.png')).exists())
    check('文件本体在老式目录', all(f.is_file() for f in files.values()))

    print('=== 2. 迁移归位：坏路径记录借 URL 兜底被修正 ===', flush=True)
    res = cs.migrate_legacy_outputs()
    check('迁移处理了 2 条记录', res['moved'] == 2, f"moved={res['moved']}")
    for page in (1, 2):
        rd = crepo.list_renders(pid, limit=10)[page - 1]
        check(f'第 {page} 页记录指向项目目录', target in Path(rd['path']).parents,
              rd['path'][-60:])
        check(f'第 {page} 页文件已在项目目录', Path(rd['path']).is_file())
    check('老式目录已清空', not any(old_dir.glob('*.png')) if old_dir.exists() else True)

    print('=== 3. 导出 PDF：不再报「成品页文件缺失」===', flush=True)
    exp = cs.export_project_pdf(pid)
    pdf = Path(exp['path'])
    check('导出成功且页数正确', exp['pages'] == 2 and pdf.is_file(),
          f"pages={exp['pages']} size={exp.get('size')}")
    check('PDF 落在项目目录', pdf.parent == target, str(pdf.parent.relative_to(OUT)))
    check('PDF 头部合法', pdf.read_bytes()[:4] == b'%PDF')

    print('=== 4. 迁移幂等 ===', flush=True)
    res2 = cs.migrate_legacy_outputs()
    check('再跑一次不再动作', res2['moved'] == 0, f"moved={res2['moved']}")

    print('=== 5. 记录在、文件已在项目目录（只修记录不搬家）===', flush=True)
    f3 = fake_render_png(target, 'p3.png')
    crepo.create_render(pid, path=str(GHOST / pid / 'p3.png'), url=relative_url(f3),
                        layout='grid_2x2', page=3, width=120, height=160)
    res3 = cs.migrate_legacy_outputs()
    check('仅修记录计 1 次', res3['moved'] == 1, f"moved={res3['moved']}")
    rds = {r['page']: r for r in crepo.list_renders(pid, limit=10)}
    check('文件没有被复制成两份', len(list(target.glob('p3.png'))) == 1)

    exp2 = cs.export_project_pdf(pid, rerender=False)
    check('三页齐导出', exp2['pages'] == 3, f"pages={exp2['pages']}")

    exp_bw = cs.export_project_pdf(pid, rerender=False, color_mode='bw')
    check('黑白版导出成功且文件名带标识',
          '黑白版' in exp_bw['filename'] and Path(exp_bw['path']).is_file(),
          exp_bw['filename'])

    print('=== 6. 相对路径存储口径 ===', flush=True)
    from app.config import settings as _settings
    from app.models_util import resolve_stored, stored_path
    # 项目根之下的文件 → data/... 相对路径；根之外（如测试临时目录）→ 退回绝对
    probe_in = Path(_settings.project_root) / 'data' / '__probe__.png'
    check('项目根下存为相对路径', stored_path(probe_in) == 'data/__probe__.png',
          stored_path(probe_in))
    check('项目根外退回绝对路径', stored_path(Path('Q:/elsewhere/x.png')) == 'Q:/elsewhere/x.png')
    rds = {r['page']: r for r in crepo.list_renders(pid, limit=10)}
    ok_file = all((resolve_stored(r['path']) or Path()).is_file() for r in rds.values())
    check('所有记录可还原并找到文件', ok_file)


main()
print()
print('结果:', 'ALL PASS' if not fails else f'{len(fails)} 项失败: {fails}')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
