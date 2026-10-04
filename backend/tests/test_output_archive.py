"""端到端验证：漫画出图按项目分目录（mock 引擎真实落盘）。

运行：backend> ..\.venv\Scripts\python.exe -u tests\test_output_archive.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='outdir_test_'))
os.environ['ENGINE_MODE'] = 'mock'
os.environ['DB_PATH'] = str(TMP / 't.db')
os.environ['DATA_DIR'] = str(TMP / 'data')
os.environ['OUTPUT_DIR'] = str(TMP / 'data' / 'outputs')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as appdb  # noqa: E402
appdb.db  # noqa: E402  触发模块级单例（首次 read/write 自动建库并跑迁移）

from app import repo  # noqa: E402
from app import comic_repo as crepo  # noqa: E402
from app.inference import scheduler  # noqa: E402
from app.models_util import output_subdir_path  # noqa: E402
from app.services import comic as cs  # noqa: E402
from app.services import generation  # noqa: E402

OUT = Path(os.environ['OUTPUT_DIR'])
fails = []


def check(name, ok, extra=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'  {extra}' if extra else ''), flush=True)
    if not ok:
        fails.append(name)


async def wait_job(job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = repo.get_job(job_id)
        if job and job.get('status') in ('succeeded', 'failed'):
            return job
        await asyncio.sleep(0.2)
    return None


async def main():
    consumer = asyncio.create_task(scheduler.consume_loop(generation.run_job))

    print('=== 1. output_subdir_path 边界 ===', flush=True)
    d = output_subdir_path(None)
    check('无 subdir → 按日期目录', d.parent == OUT and len(d.name) == 10,
          str(d.relative_to(OUT)))
    check('有 subdir → 项目目录', output_subdir_path('comics/测试_abc123') == OUT / 'comics' / '测试_abc123')
    for evil in ('../escape', '/abs/path', 'a/../../b', '..\\win'):
        de = output_subdir_path(evil)
        check(f'越界 subdir 被拦下: {evil!r}',
              str(OUT.resolve()) in str(de.resolve()), str(de.relative_to(OUT)))

    print('=== 2. project_output_subdir 生成与持久化 ===', flush=True)
    pid = crepo.create_project(title='雨夜/天台:追逐?', style='jp_bw', layout='grid_2x2',
                               width=768, height=768, steps=4, guidance=4.0)['id']
    proj = crepo.get_project(pid)
    sub = cs.project_output_subdir(proj)
    leaf = sub.split('/', 1)[1]           # 只看目录名本身（前缀 comics/ 含 /）
    print('   目录名:', sub, flush=True)
    check('目录名不含非法字符', not any(c in leaf for c in '\\/:*?"<>|'))
    check('落在 comics/ 下', sub.startswith('comics/'))
    check('已持久化到项目', (crepo.get_project(pid).get('output_dir') or '') == sub)
    check('再次调用幂等', cs.project_output_subdir(crepo.get_project(pid)) == sub)
    crepo.update_project(pid, title='雨夜天台（改名后）')
    check('改名后目录不变', cs.project_output_subdir(crepo.get_project(pid)) == sub)

    print('=== 3. 重名项目不会撞目录 ===', flush=True)
    pid2 = crepo.create_project(title='雨夜/天台:追逐?', width=768, height=768, steps=4)['id']
    sub2 = cs.project_output_subdir(crepo.get_project(pid2))
    check('同名项目目录不同', sub2 != sub, f'{sub} vs {sub2}')

    print('=== 4. 端到端：角色立绘落到项目目录 ===', flush=True)
    cid = crepo.create_character(pid, name='林晚', gender='女', age='20 岁',
                                 appearance='黑长直', outfit='校服')['id']
    r = await cs.enqueue_character_image(cid)
    job = await wait_job(r['job_id'])
    check('角色出图任务成功', bool(job and job.get('status') == 'succeeded'),
          (job or {}).get('status', 'timeout'))
    proj_dir = OUT / sub
    files = sorted(proj_dir.rglob('*.png'))
    check('立绘落在项目目录内', len(files) >= 1,
          f'{len(files)} 个文件: ' + ', '.join(f.name[:44] for f in files[:3]))
    ch = crepo.get_character(cid)
    from app.models_util import resolve_stored
    ch_path = resolve_stored(ch.get('ref_path'))
    check('角色记录指向项目目录（相对路径可还原）',
          ch_path is not None and ch_path.parent == proj_dir,
          (ch.get('ref_path') or ''))

    print('=== 5. 场景图同样归档 ===', flush=True)
    sid = crepo.create_scene(pid, name='天台', location='楼顶', time_of_day='夜')['id']
    rs = await cs.enqueue_scene_image(sid)
    js = await wait_job(rs['job_id'])
    check('场景出图任务成功', bool(js and js.get('status') == 'succeeded'),
          (js or {}).get('status', 'timeout'))
    sc = crepo.get_scene(sid)
    check('场景记录指向项目目录', sub.replace('/', os.sep) in (sc.get('image_url') or '')
          or f'comics/{leaf}' in (sc.get('image_url') or ''), (sc.get('image_url') or '')[-60:])

    print('=== 6. 主界面普通出图仍按日期目录 ===', flush=True)
    payload = {
        'job_id': 'job_probe', 'prompt': 'a cat', 'negative_prompt': '', 'width': 256,
        'height': 256, 'steps': 1, 'guidance_scale': 4.0, 'seed': 1, 'batch_size': 1,
        'count': 1, 'mode': 'txt2img', 'strength': 0.6, 'channel': 'auto',
    }
    await generation.submit_batch(payload)
    jb = await wait_job(payload['job_id'])
    check('主界面任务成功', bool(jb and jb.get('status') == 'succeeded'),
          (jb or {}).get('status', 'timeout'))
    today = OUT / time.strftime('%Y-%m-%d')
    check('普通出图落在日期目录', today.exists() and any(today.rglob('*.png')),
          str(today.relative_to(OUT)))

    print('=== 7. 历史出图归位（旧日期目录 → 项目目录）===', flush=True)
    from PIL import Image

    from app.models_util import relative_url

    date_dir = OUT / '2026-01-01'
    date_dir.mkdir(parents=True, exist_ok=True)
    legacy_char = date_dir / 'old_char.png'
    legacy_scene = date_dir / 'old_scene.png'
    legacy_plain = date_dir / 'plain_user_image.png'   # 无项目归属，绝不能被搬走
    for p, color in ((legacy_char, (10, 20, 30)), (legacy_scene, (30, 20, 10)),
                     (legacy_plain, (0, 0, 0))):
        Image.new('RGB', (64, 64), color).save(p)

    crepo.update_character(cid, ref_path=str(legacy_char), ref_url=relative_url(legacy_char))
    crepo.update_scene(sid, image_url=relative_url(legacy_scene))

    res = cs.migrate_legacy_outputs()
    check('迁移确实搬了文件', res['moved'] >= 2, f"moved={res['moved']}")
    ch2 = crepo.get_character(cid)
    check('角色立绘已归位且 DB 已更新',
          (proj_dir / legacy_char.name).exists() and legacy_char.name in (ch2.get('ref_path') or ''),
          (ch2.get('ref_path') or '')[-50:])
    sc2 = crepo.get_scene(sid)
    check('场景图已归位且 DB 已更新',
          (proj_dir / legacy_scene.name).exists() and legacy_scene.name in (sc2.get('image_url') or ''),
          (sc2.get('image_url') or '')[-50:])
    check('无项目归属的图不受影响', legacy_plain.exists())
    check('日期目录里的项目图已移走', not legacy_char.exists() and not legacy_scene.exists())
    res2 = cs.migrate_legacy_outputs()
    check('迁移幂等（再跑一次不动）', res2['moved'] == 0, f"第二次 moved={res2['moved']}")

    consumer.cancel()

    print('=== 目录结构 ===', flush=True)
    for p in sorted(OUT.rglob('*')):
        if p.is_dir():
            n = len(list(p.glob('*.png')))
            print(f'   {p.relative_to(OUT)}/  ({n} png)', flush=True)


asyncio.run(main())
print()
print('结果:', 'ALL PASS' if not fails else f'{len(fails)} 项失败: {fails}')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
