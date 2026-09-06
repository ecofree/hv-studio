"""
命令行入口。提供子命令：
  episode  init/show/produce/reshot/shots/checkpoint/audit-wav
           run = 已废弃（2026-09-05 双轨编排器删除，仅打印指路提示后 exit 1）
  relay    status/sync/pick
  clean    --all | --episode ID  [--segments] [--apply]
                                 build/ 中间产物清理（默认 dry-run；图片资产永不删）
  image    test                  真实最小生图
  polyphone  text/stats/rules    多音字预处理
  cost     [--since D] [--raw]   花销对账（金额需自建 state/cost_prices.json）
  config   show                  打印当前配置
  webui / api                    启动 Streamlit / FastAPI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 python -m app.cli 在工程根目录执行时能找到包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_episode(args):
    from app.managers import load, save, checkpoint
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
        # C3（2026-09-05）：双轨编排器 app/managers/pipeline.py 已删。
        # `run` 只保留一个版本的路标，不再静默执行任何东西——直接退出，
        # 避免「以为跑了其实跑的是另一套逻辑」这种最坏情况。
        raise SystemExit(
            "[deprecated] `hv episode run` 已移除（旧的双轨编排器 pipeline.py 已删）。\n"
            "  全流程重跑 → hv episode produce <id> [--stage tts] [--no-fallback]\n"
            "  单镜重建   → hv episode reshot <id> --key LGT01 [--key LGT02]\n"
            "  分镜装配   → hv episode shots <id> [--dry-run]")
    elif args.action == "reshot":
        # C3：单镜重建（删旧图 → 只重生成这几个 key → 重新装配 → 重新合成）。
        # 这是旧 `run --only` 的唯一替代路径，实现在 orchestrator.reshot。
        from app.orchestrator import reshot
        import json
        keys = args.key or []
        if not keys:
            raise SystemExit("error: reshot 至少一个 --key（底图 key，如 LGT01）")
        res = reshot(args.episode_id, keys,
                     do_verify=not getattr(args, "no_verify", False))
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif args.action == "produce":
        # P0-3：真实生产链（script→tts→compose→verify），状态由产物推进。
        from app.orchestrator import produce
        import json
        # --no-fallback（B2-3）：cosyvoice 失败不再静默降级 edge。
        # 默认降级是为了「跑得完」，但代价是一部片子里混两种声音且无声无息；
        # 交付集应显式关掉，让失败暴露在 TTS 阶段而不是等成片后被人耳发现。
        res = produce(args.episode_id, stages=args.stage or None,
                      tts_fallback=not args.no_fallback)
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


def cmd_clean(args):
    """build/ 中间产物清理（P1-8）。默认 dry-run，--apply 才真删。

    三条硬约束（见 app/services/cleanup.py）：
      1) 白名单——只清 compose/调试的中间产物，绝不 glob 后 rm -rf
      2) 图片资产永不删——底图是花钱生成且跨集复用的
      3) 默认 dry-run——删之前必须让人看见清单和体积

    段（build/segments/seg-*.mp4）默认不清：删了下次要全量重编，
    所以它是一等公民缓存，要显式 --segments 才动。
    """
    from app.paths import episodes_dir
    from app.services.cleanup import clean_episodes, _human

    eps = args.episode or []
    if not args.all and not eps:
        raise SystemExit(
            "error: 必须指定范围 —— --all（全部集）或 --episode <id>（可多次）\n"
            "  hv clean --all                 # dry-run，只看能清出多少\n"
            "  hv clean --all --apply         # 真删（build 中间产物）\n"
            "  hv clean --all --segments --apply   # 连段一起删（下次全量重编）")

    root = episodes_dir()
    if args.all:
        ids = sorted(p.stem for p in root.glob("*.json"))
    else:
        ids = list(eps)
    ep_dirs = {}
    for eid in ids:
        d = root / eid
        if d.is_dir():
            ep_dirs[eid] = d
        elif not (root / f"{eid}.json").exists():
            print(f"  [!] 跳过 {eid}：episodes/{eid}/ 与 {eid}.json 都不存在")
    if not ep_dirs:
        raise SystemExit("error: 没有匹配到任何集")

    res = clean_episodes(ep_dirs, include_build=True,
                         include_segments=args.segments, apply=args.apply)

    mode = "已删(apply)" if args.apply else "可清(dry-run)"
    print(f"[clean] 范围 {len(ep_dirs)} 集  档位 "
          f"{'build + segments' if args.segments else 'build（不含段）'}")
    for it in res["items"]:
        print(f"  [{it['tier']:8s}] {_human(it['size']):>9s}  {it['path']}")
    if res["skipped"]:
        print(f"[clean] 被硬闸拦下 {len(res['skipped'])} 项（永不删除）：")
        for s in res["skipped"][:10]:
            print(f"  [skip] {s['path']}  —— {s['reason']}")
    print(f"[clean] {mode}: {len(res['items'])} 项 / "
          f"{_human(res['total_bytes'])}"
          + (f" → 实删 {res['deleted']} 项 / {_human(res['deleted_bytes'])}"
             if args.apply else " （加 --apply 才真删）"))
    if not args.apply and args.segments:
        print("[clean] 注意：段是断点续跑的缓存，删后下次 compose 会全量重编。")


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
    """启动 FastAPI。非回环 host 且未配 HV_API_TOKEN → 拒绝启动。

    这是 B1 token 闸的另一半：`--host 0.0.0.0` 会把含付费生图的端点暴露到网卡，
    而 token 是 opt-in（不配就不校验）。两者同时发生时等于裸奔，所以在启动侧
    硬拦——「忘了配 token」是最可能发生的一种失误，不能靠人记得。
    """
    import uvicorn
    from app.controllers.v1.auth import auth_enabled, is_loopback_host

    if not is_loopback_host(args.host) and not auth_enabled():
        raise SystemExit(
            f"error: --host {args.host} 会暴露含付费端点（/v1/image/test、"
            f"/produce?stage=assets），但未设置环境变量 HV_API_TOKEN。\n"
            f"  要么改回默认 --host 127.0.0.1；\n"
            f"  要么设置 HV_API_TOKEN 后重启（客户端需带 X-HV-Token 头）。")
    if not auth_enabled():
        print("[api] 提示：未设 HV_API_TOKEN，写/付费端点不校验"
              "（仅监听本机时无妨）。")
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
    # 注：run 已废弃（C3），仍留在 choices 里只为打印一段指路提示，不执行任何逻辑。
    p.add_argument("action", choices=["init", "show", "run", "produce", "reshot",
                                      "shots", "checkpoint", "audit-wav"],
                   help="init/show/produce/reshot/shots/checkpoint/audit-wav；"
                        "run=已废弃(仅打印指路提示)；produce=真实生产链；"
                        "reshot=单镜重建(删图+重生成+重合成)；"
                        "shots=按 scene_plan 装配；"
                        "audit-wav=孤儿wav清理(默认dry-run)")
    p.add_argument("episode_id", nargs="?", default=None,
                   help="episode id（audit-wav 配 --all 时可省略）")
    p.add_argument("--label", default="manual")
    p.add_argument("--apply", action="store_true",
                   help="[audit-wav] 实际删除（默认只 dry-run 列出）")
    p.add_argument("--all", action="store_true",
                   help="[audit-wav] 扫全部集（省略 episode_id）")
    p.add_argument("--key", action="append", default=None,
                   help="[reshot] 要重建的底图 key，可多次：--key LGT01 --key LGT02")
    p.add_argument("--no-verify", action="store_true",
                   help="[reshot] 合成后跳过 verify（默认会跑时序断言）")
    p.add_argument("--stage", action="append", default=None,
                   help="[produce] 指定阶段，可多次：--stage tts --stage compose")
    p.add_argument("--no-fallback", action="store_true",
                   help="[produce] cosyvoice 失败时不降级 edge（宁可失败也不混"
                        "音色；交付集建议加）")
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

    # clean：build/ 中间产物清理（P1-8）
    p = sub.add_parser("clean", help="清理 build/ 中间产物（默认 dry-run）")
    p.add_argument("--all", action="store_true", help="扫全部集")
    p.add_argument("--episode", action="append", default=None, metavar="ID",
                   help="指定集，可多次：--episode hw-001 --episode lsm-15min")
    p.add_argument("--segments", action="store_true",
                   help="连 build/segments/seg-*.mp4 一起清（删后下次全量重编）")
    p.add_argument("--apply", action="store_true",
                   help="实际删除（默认只列出清单与体积）")
    p.set_defaults(func=cmd_clean)

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
