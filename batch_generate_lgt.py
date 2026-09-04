"""
Batch generate LGT images for an episode's image_prompts.json.

薄封装：自 hv P1 起，生图逻辑单源化到 app.orchestrator.build_image_jobs
（prompt = en + style_suffix + negative，扁平 ImageConfig 顶层字段），生成走
app.services.image.generate_many（三路并行 + 幂等跳过 + 失败换路）。
本脚本只保留 batch 语义：写 generate.log、逐张 ok/fail 计数、单张失败不中断，
不再维护第二份 prompt 拼接（历史双份逻辑曾漂移）。

用法：
    python batch_generate_lgt.py [episode_id] [--force]
  episode_id 默认 laogongtuan；--force 无视幂等重生成全部。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

ROOT = Path("D:/hv")


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    ep_id = argv[0] if argv else "laogongtuan"
    force = "--force" in sys.argv

    from app.orchestrator import build_image_jobs
    from app.services.image import generate_many
    from app.managers import load

    ep = load(ep_id)
    jobs = build_image_jobs(ep.id)
    if not jobs:
        print(f"[batch] {ep_id} 无 image_prompts.json 或全空，无事可做")
        return 0

    # 幂等跳过计数（与 generate_many 内部判据一致：已存在且>50KB）
    skip = sum(1 for j in jobs if (not force) and j.is_done())
    print(f"[batch] {ep_id}: {len(jobs)} LGT, todo={len(jobs)-skip}, skip={skip}")

    log_p = ROOT / "episodes" / ep_id / "generate.log"
    log_p.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    with open(log_p, "a", encoding="utf-8") as log:
        log.write(f"\n=== BATCH START {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"| total {len(jobs)} | todo {len(jobs)-skip} | force={force} ===\n")

        if (not force) and skip == len(jobs):
            # 全部幂等命中 → 不建 relay、不触渠道（省去 get_image 的初始化/健康探针）
            for j in jobs:
                sz = Path(j.out_path).stat().st_size if Path(j.out_path).exists() else 0
                log.write(f"{j.id} skip {sz/1024:.0f}KB\n")
                print(f"  {j.id} skip (exists {sz/1024:.0f}KB)")
            dt = time.time() - start
            log.write(f"=== BATCH END ok=0 skip={skip} fail=0 {dt:.0f}s ===\n")
            print(f"\nDONE ok=0 skip={skip} fail=0 in {time.time()-start:.0f}s")
            return 0

        res = generate_many(jobs, force=force)

        ok = fail = 0
        for j in jobs:
            path = res.get(j.id)
            err = res.get(f"{j.id}__error")
            if path:
                # _attempts==0 且已存在 → 幂等命中(未真请求)，记 skip；否则记 ok
                gen = bool(getattr(j, "_attempts", 0))
                sz = Path(path).stat().st_size if Path(path).exists() else 0
                if gen:
                    ok += 1
                    log.write(f"{j.id} ok {sz/1024:.0f}KB\n")
                    print(f"  {j.id} ok {sz/1024:.0f}KB")
            else:
                fail += 1
                log.write(f"{j.id} FAIL {(err or 'unknown')[:120]}\n")
                print(f"  {j.id} FAIL {(err or 'unknown')[:120]}")

        dt = time.time() - start
        log.write(f"=== BATCH END ok={ok} skip={skip} fail={fail} {dt:.0f}s ===\n")
    print(f"\nDONE ok={ok} skip={skip} fail={fail} in {time.time()-start:.0f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
