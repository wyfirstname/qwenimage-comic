r"""验证任务参数大字段外置（app/blobs.py）+ 历史库瘦身迁移。

事故背景：分镜融合任务把参考图 base64 写进 jobs.params_json（单条 4~5MB），
本机 jobs 表涨到 151MB 并把 -wal 写坏。这里确保：
  * 新任务的 params_json 只留 @blob 引用，体积回到 KB 级；
  * 执行时能原样还原，融合通道拿到的参考图一张不少；
  * 任务结束后临时文件被清掉；
  * 历史超大参数快照在启动迁移里被清理。

运行：backend> ..\.venv\Scripts\python.exe -u tests\test_job_payload_blobs.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='blobs_test_'))
os.environ['ENGINE_MODE'] = 'mock'
os.environ['DB_PATH'] = str(TMP / 't.db')
os.environ['DATA_DIR'] = str(TMP / 'data')
os.environ['OUTPUT_DIR'] = str(TMP / 'data' / 'outputs')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import blobs  # noqa: E402
from app import db as appdb  # noqa: E402
appdb.db  # noqa: E402  触发模块级单例（首次 read/write 自动建库并跑迁移）

from app import comic_repo as crepo  # noqa: E402
from app import repo  # noqa: E402
from app.inference import scheduler  # noqa: E402
from app.services import comic as cs  # noqa: E402
from app.services import generation  # noqa: E402

fails = []


def check(name, ok, extra=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'  {extra}' if extra else ''), flush=True)
    if not ok:
        fails.append(name)


def params_len(job_id):
    with appdb.db.read() as conn:
        row = conn.execute("SELECT LENGTH(params_json) AS n FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
    return int(row['n']) if row else -1


async def wait_job(job_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        job = repo.get_job(job_id)
        if job and job.get('status') in ('succeeded', 'failed'):
            return job
        await asyncio.sleep(0.2)
    return None


async def main():
    consumer = asyncio.create_task(scheduler.consume_loop(generation.run_job))

    print('=== 1. pack / unpack 基本行为 ===', flush=True)
    big = 'A' * 300000
    payload = {'prompt': 'short', 'image': 'data:image/png;base64,' + big,
               'ref_images': ['data:image/png;base64,' + big], 'nested': {'k': big},
               'count': 1}
    packed = blobs.pack(payload, 'job_probe1')
    check('原对象未被修改', payload['image'].endswith(big))
    check('短字符串保持原样', packed['prompt'] == 'short')
    check('长字符串换成 @blob 引用', str(packed['image']).startswith('@blob:job_probe1/'),
          str(packed['image']))
    check('列表内的长字符串同样外置', str(packed['ref_images'][0]).startswith('@blob:'))
    check('嵌套字段也处理', str(packed['nested']['k']).startswith('@blob:'))
    check('外置后体积很小', len(json.dumps(packed)) < 500, str(len(json.dumps(packed))))
    restored = blobs.unpack(packed)
    check('unpack 还原图片数据', restored['image'] == payload['image'])
    check('unpack 还原列表', restored['ref_images'] == payload['ref_images'])
    check('unpack 还原嵌套', restored['nested']['k'] == big)
    blob_files = list((TMP / 'data' / 'blobs' / 'job_probe1').iterdir())
    check('blob 文件已落盘', len(blob_files) == 3, str([p.name for p in blob_files]))
    blobs.release('job_probe1')
    check('release 清掉目录', not (TMP / 'data' / 'blobs' / 'job_probe1').exists())

    print('=== 2. 引用丢失时不静默（保留 token 让上层报错）===', flush=True)
    orphan = {'image': '@blob:job_gone/0'}
    check('缺失的 blob 保留 token', blobs.unpack(orphan)['image'] == '@blob:job_gone/0')
    for evil in ('@blob:../../etc/0', '@blob:/abs/0', '@blob:job/../0', '@blob:job'):
        check(f'非法 token 不被解析: {evil}', blobs.unpack({'x': evil})['x'] == evil)

    print('=== 3. 真实分镜融合任务：参数不再入库图片 ===', flush=True)
    pid = crepo.create_project(title='外地出差', style='jp_color', width=256, height=256,
                              steps=1, guidance=4.0)['id']
    cid = crepo.create_character(pid, name='林晚', gender='女', age='20 岁',
                                 appearance='黑长直', outfit='风衣')['id']
    r = await cs.enqueue_character_image(cid)
    job = await wait_job(r['job_id'])
    check('角色立绘出图成功', bool(job and job['status'] == 'succeeded'),
          (job or {}).get('status', 'timeout'))
    check('立绘任务参数很小', 0 < params_len(r['job_id']) < 20000, f"{params_len(r['job_id'])}B")
    check('立绘任务 blob 已清理',
          not (TMP / 'data' / 'blobs' / r['job_id']).exists())

    sid = crepo.create_scene(pid, name='出租屋', location='卧室', time_of_day='夜')['id']
    rs = await cs.enqueue_scene_image(sid)
    js = await wait_job(rs['job_id'])
    check('场景图出图成功', bool(js and js['status'] == 'succeeded'),
          (js or {}).get('status', 'timeout'))

    panel = crepo.create_panel(pid, seq=1, page=1, cell=0, shot='wide',
                              scene='出租屋', dialogue='林晚：「灯还亮着。」',
                              character_ids=[cid])
    rp = await cs.enqueue_panel(panel['id'])
    jp = await wait_job(rp['job_id'])
    check('分镜（融合通道）出图成功', bool(jp and jp['status'] == 'succeeded'),
          (jp or {}).get('status', 'timeout') or (jp or {}).get('error', ''))
    plen = params_len(rp['job_id'])
    check('分镜任务参数压缩到 KB 级（原来 4~5MB）', 0 < plen < 20000, f'{plen}B')
    with appdb.db.read() as conn:
        row = conn.execute('SELECT params_json, mode FROM jobs WHERE id = ?',
                           (rp['job_id'],)).fetchone()
    check('库里存的是 @blob 引用', '@blob:' in row['params_json'])
    check('模式仍是 img2img（融合通道没变）', row['mode'] == 'img2img', row['mode'])
    check('分镜已回写图片', bool(crepo.get_panel(panel['id']).get('image_url')))
    check('分镜任务 blob 已清理',
          not (TMP / 'data' / 'blobs' / rp['job_id']).exists())

    print('=== 4. 启动清理残留 blob ===', flush=True)
    leftover = TMP / 'data' / 'blobs' / 'job_old_ghost'
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / '0').write_text('x', encoding='utf-8')
    removed = blobs.gc_all()
    check('gc 清掉残留目录', removed >= 1 and not leftover.exists(), f'removed={removed}')

    print('=== 5. 历史超大参数快照迁移 ===', flush=True)
    legacy_id = 'job_legacy_big'
    legacy_payload = {
        'job_id': legacy_id, 'prompt': 'a cat', 'image': 'data:image/png;base64,' + 'B' * 400000,
        'ref_images': ['data:image/png;base64,' + 'B' * 400000], 'width': 768, 'height': 768,
    }
    with appdb.db.write() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, mode, prompt, negative, params_json, seed, count,"
            " progress, message, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (legacy_id, 'succeeded', 'img2img', 'a cat', '', json.dumps(legacy_payload),
             -1, 1, 100, '完成', '2026-01-01T00:00:00'),
        )
    check('历史行确实是超大参数', params_len(legacy_id) > 200000, f'{params_len(legacy_id)}B')

    shrank = appdb.db._shrink_oversized_job_params(appdb.db._write_conn())
    appdb.db._write_conn().commit()
    check('迁移报告已清理', shrank is True)
    after = params_len(legacy_id)
    check('历史行被压缩', after < 20000, f'{after}B')
    with appdb.db.read() as conn:
        text = conn.execute('SELECT params_json FROM jobs WHERE id = ?',
                            (legacy_id,)).fetchone()['params_json']
    check('保留了真正的参数', json.loads(text)['prompt'] == 'a cat')
    check('图片位置换成占位说明', '已省略' in text, text[:120])
    check('再次运行幂等', appdb.db._shrink_oversized_job_params(appdb.db._write_conn()) is False)

    consumer.cancel()

    print('=== 数据库实际占用 ===', flush=True)
    print(f'   {os.environ["DB_PATH"]} = {Path(os.environ["DB_PATH"]).stat().st_size} B', flush=True)
    total_params = 0
    with appdb.db.read() as conn:
        total_params = conn.execute('SELECT SUM(LENGTH(params_json)) FROM jobs').fetchone()[0]
    print(f'   所有任务参数合计 = {total_params} B', flush=True)


asyncio.run(main())
print()
print('结果:', 'ALL PASS' if not fails else f'{len(fails)} 项失败: {fails}')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
