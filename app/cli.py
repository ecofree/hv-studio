"""
命令行入口。提供子命令：
  episode  init/show/run/checkpoint
  relay    status/sync/pick
  image    test                  真实最小生图
  polyphone  text/stats/rules    多音字预处理
  config   show                  打印当前配置
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 python -m app.cli 在工程根目录执行时能找到包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_episode(args):
    from app.managers import load, save, run_stage, checkpoint
    from app.managers.pipeline import run_episode
    from app.models import Episode

    # audit-wav 是唯一可用 --all 扫全部集而不需要单集的 action
    if args.action != "audit-wav" and not args.episode_id:
        raise SystemExit("error: episode_id 必填（audit-wav 加 --all 可省略）")

    if args.action == "init":
        ep = Episode(id=args.episode_id, title=args.episode_id, episode_number=0)
        p = save(ep)
        print(f"created: {p}")
    elif args.action == "show":
        ep = load(args.episode_id)
        import json
        print(json.dumps(ep.model_dump(mode="json"), ensure_ascii=False, indent=2))
    elif args.action == "checkpoint":
        ep = load(args.episode_id)
        p = checkpoint(ep, args.label)
        print(f"checkpoint: {p}")
    elif args.action == "run":
        # M2：pipeline 编排器。--only 单镜头重建；--stage 指定阶段；
        # --force 强制重做；--compose 单镜改图后重合成整片+时序断言。
        res = run_episode(
            args.episode_id,
            only=args.only,
            stages=args.stage or None,
            force=args.force,
            compose_after=args.compose,
        )
        for sid, done in res.items():
            print(f"  {sid}: 完成 {done}")
    elif args.action == "produce":
        # P0-3：真实生产链（script→tts→compose→verify），状态由产物推进。
        from app.orchestrator import produce
        import json
        res = produce(args.episode_id, stages=args.stage or None)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif args.action == "audit-wav":
        # 孤儿 wav 清理（opt-in）：默认只列，--apply 才删。
        # --all 扫全部集（此时 episode_id 参数忽略）。
        from app.managers.episode import audit_orphan_wavs
        apply = bool(getattr(args, "apply", False))
        targets = ([e.stem for e in Path("episodes").glob("*.json")]
                   if getattr(args, "all", False) else [args.episode_id])
        tot = {"deleted": 0, "skipped": 0, "orphans": []}
        for eid in targets:
            r = audit_orphan_wavs(eid, apply=apply)
            tot["deleted"] += r["deleted"]
            tot["skipped"] += r["skipped"]
            tot["orphans"].extend(r["orphans"])
            if r["orphans"]:
                verb = "已删" if apply else "待删(dry-run)"
                print(f"  [{eid}] 孤儿 {len(r['orphans'])} 个 -> {verb} "
                      f"(deleted={r['deleted']})")
        mode = "apply" if apply else "dry-run"
        print(f"[audit-wav] {mode}: 孤儿合计 {len(tot['orphans'])} "
              f"(deleted={tot['deleted']} skipped={tot['skipped']})")
    elif args.action == "shots":
        # 通用「剧本→分镜」：按 episodes/{id}/scene_plan.json 声明式装配 shots。
        from app.services.shot_builder import build_episode_shots, inspect_episode
        from app.managers import load
        plan = Path(args.plan) if getattr(args, "plan", None) else None
        ep = load(args.episode_id)
        if not ep.frames:
            print(f"[!] episode {args.episode_id} 无 frames，先跑 script 切分")
            return
        # 先用非落盘 dry-run 校验 scene_plan 覆盖/底图/grade/帧指纹，全通过才落盘
        strict_fp = getattr(args, "strict_fingerprint", False)
        _, st = build_episode_shots(args.episode_id, plan_path=plan,
                                    persist=False, preview_rows=args.preview,
                                    strict_fingerprint=strict_fp)
        fp = st.get("fingerprint")
        fp_txt = (f" / 帧指纹(已锚定) {fp['count']}帧 sha1={fp['sha1']}" if fp
                  else " / 帧指纹(缺失·未校验)")
        print(f"[shots] dry-run OK：{st['shots']} shots / {st['base_images']} 底图 / "
              f"归并 {st['reuse_ratio']}:1{fp_txt}")
        if not args.dry_run:
            ep2, st2 = build_episode_shots(args.episode_id, plan_path=plan,
                                           persist=True, preview_rows=0,
                                           strict_fingerprint=strict_fp)
            print(f"[shots] 已落盘：{ep2.id} → {st2['shots']} shots")
            problems = inspect_episode(args.episode_id)
            print(f"[shots] schema: {'OK' if not problems else problems}")


def cmd_relay(args):
    from app.managers import get_relay
    import json
    r = get_relay()
    if args.action == "status":
        print(json.dumps(r.snapshot(), ensure_ascii=False, indent=2))
    elif args.action == "sync":
        r.sync_quota(force=True)
        print(json.dumps(r.snapshot(), ensure_ascii=False, indent=2))
    elif args.action == "pick":
        c = r.pick()
        print(f"picked: {c.name}  base={c.base_url}")


def cmd_image(args):
    import time, base64
    from app.services.image import get_image, save_b64_image
    from app.models import ImageGenRequest, predict_actual_dims

    img = get_image()
    req = ImageGenRequest(
        model=args.model, prompt=args.prompt,
        size=args.size, quality=args.quality,
    )
    w, h = predict_actual_dims(args.size)
    print(f"请求 size={args.size}  → 预测实际 {w}x{h}")
    t0 = time.time()
    resp = img.generate(req)
    dt = time.time() - t0
    item = resp.data[0]
    print(f"生图 {dt:.1f}s  revised_prompt={(item.revised_prompt or '')[:80]}")
    if item.b64_json:
        out = args.out or f"assets/probe/cli_{int(time.time())}.png"
        p = save_b64_image(item.b64_json, out)
        print(f"已保存: {p}  ({Path(p).stat().st_size/1024:.0f}KB)")


def cmd_cost(args):
    """生图台账：花了多少张、走的哪条路、什么规格。

    金额只在配置 state/cost_prices.json 后才有；没配也能精确回答
    「每张图走的哪个渠道池」——这才是三池对账真正要的。
    """
    from app.services.image import cost_summary, _COST_PATH
    s = cost_summary(since=args.since)
    print(f"台账文件: {_COST_PATH}")
    print(f"总张数: {s['total_images']}" + (f"（ts >= {args.since}）" if args.since else ""))
    for name, key in (("按渠道", "by_channel"), ("按规格", "by_spec"),
                      ("按集", "by_episode")):
        d = s[key]
        if not d:
            continue
        print(f"{name}:")
        for k, v in d.items():
            print(f"    {str(k):32s} {v:6d}")
    if s["cost_total"] is not None:
        print(f"金额合计: {s['cost_total']}")
    else:
        print(f"金额合计: 未配置（{s['unpriced']} 条无单价，"
              f"填 {s['prices_file']} 即可）")
    if args.raw:
        import json as _j
        print(_j.dumps(_j.loads(_COST_PATH.read_text(encoding="utf-8"))
                       if _COST_PATH.exists() else [],
                       ensure_ascii=False, indent=1))


def cmd_polyphone(args):
    from app.services.polyphone import preprocess, hit_stats, rules
    if args.text:
        print(preprocess(args.text))
    elif args.stats:
        for k, v in sorted(hit_stats().items(), key=lambda kv: -kv[1]):
            print(f"{k:8s} → {v:5d}")
    elif args.rules:
        for k, v in rules().items():
            print(f"{k:8s} → {v}")
    else:
        for s in ["张骞被匈奴扣押十年", "单于冒顿派阏氏看守"]:
            print(f"  {s}\n  → {preprocess(s)}")


def cmd_config(_args):
    from app.config.config import get_settings
    s = get_settings()
    import json
    print(json.dumps({
        "profile": s.profile_name,
        "image_model": s.image_model,
        "image_size": s.image_size,
        "actual_dims": s.predict_actual_image_dims(),
        "upscaled_dims": s.predict_upscaled_dims(),
        "channels": [c.name for c in s.channels],
        "video": {"engine": s.video.primary.engine, "endpoint": s.video.primary.endpoint},
        "vram_GB": s.runtime.vram.measured_usable_GB,
    }, ensure_ascii=False, indent=2))


def cmd_api(args):
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


def cmd_webui(args):
    import subprocess
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", "app/webui/Main.py",
        "--server.port", str(args.port),
    ], check=False)


def main():
    ap = argparse.ArgumentParser(prog="hv")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # episode
    p = sub.add_parser("episode", help="管理 episode.json")
    p.add_argument("action", choices=["init", "show", "run", "produce", "shots",
                                      "checkpoint", "audit-wav"],
                   help="init/show/run/produce/shots/checkpoint/audit-wav；"
                        "produce=真实生产链；shots=按 scene_plan 装配；"
                        "audit-wav=孤儿wav清理(默认dry-run)")
    p.add_argument("episode_id", nargs="?", default=None,
                   help="episode id（audit-wav 配 --all 时可省略）")
    p.add_argument("--label", default="manual")
    p.add_argument("--apply", action="store_true",
                   help="[audit-wav] 实际删除（默认只 dry-run 列出）")
    p.add_argument("--all", action="store_true",
                   help="[audit-wav] 扫全部集（省略 episode_id）")
    p.add_argument("--only", default=None,
                   help="[run] 只重建某镜头，如 shot-001")
    p.add_argument("--stage", action="append", default=None,
                   help="[run] 指定阶段，可多次：--stage image --stage motion")
    p.add_argument("--force", action="store_true",
                   help="[run] 强制重做（无视幂等）")
    p.add_argument("--compose", action="store_true",
                   help="[run] 单镜改图后重合成整片 + 时序断言")
    p.add_argument("--plan", default=None,
                   help="[shots] scene_plan.json 路径（默认 episodes/{id}/scene_plan.json）")
    p.add_argument("--dry-run", action="store_true",
                   help="[shots] 只校验不落盘")
    p.add_argument("--preview", type=int, default=12,
                   help="[shots] 预览行数（0=不预览）")
    p.add_argument("--strict-fingerprint", action="store_true",
                   help="[shots] scene_plan 缺帧指纹也视为校验失败（老集默认只告警）")
    p.set_defaults(func=cmd_episode)

    # relay
    p = sub.add_parser("relay", help="subrouter 渠道池状态")
    p.add_argument("action", choices=["status", "sync", "pick"])
    p.set_defaults(func=cmd_relay)

    # image
    p = sub.add_parser("image", help="测试生图")
    p.add_argument("--prompt", default="西汉使者，戈壁黄昏，电影感，写实")
    p.add_argument("--size", default="1080x1920")
    p.add_argument("--quality", default="low")
    p.add_argument("--model", default="gpt-image-2")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_image)

    # cost：生图台账（state/cost_ledger.json）
    p = sub.add_parser("cost", help="生图成本台账汇总")
    p.add_argument("--since", default=None,
                   help="只统计 ts >= 该值（如 2026-09-04）")
    p.add_argument("--raw", action="store_true", help="打印全部原始条目")
    p.set_defaults(func=cmd_cost)

    # polyphone
    p = sub.add_parser("polyphone", help="多音字预处理")
    p.add_argument("--text", default=None)
    p.add_argument("--stats", action="store_true")
    p.add_argument("--rules", action="store_true")
    p.set_defaults(func=cmd_polyphone)

    # config
    p = sub.add_parser("config", help="打印当前配置")
    p.set_defaults(func=cmd_config)

    # api
    p = sub.add_parser("api", help="启动 FastAPI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_api)

    # webui
    p = sub.add_parser("webui", help="启动 Streamlit 控制台")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=cmd_webui)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
