"""Restore a saved image response offline, including quarantined responses."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from app import paths
    from app.orchestrator import _episode_lock, _load_script_md
    from app.managers import load
    from app.services.production_inputs import prepare
    from app.services.narration_pipeline import read
    from app.services.provenance import fingerprint
    from app.services.image import recover_saved_response
    parser=argparse.ArgumentParser(description='离线复用已收到的图片，不产生新调用')
    parser.add_argument('--episode',required=True)
    parser.add_argument('--image-key',required=True)
    parser.add_argument('--receipt',required=True,type=Path)
    parser.add_argument('--original-budget',type=Path,help='旧响应缺少调用编号时，核对原预算并关联已成功的唯一请求')
    args=parser.parse_args()
    with _episode_lock(args.episode):
        root=paths.episode_dir(args.episode)/'production/v2'
        plan_path=Path(read(root/'run.json')['plan_path'])
        ep=load(args.episode)
        if read(plan_path)['source_sha256']!=fingerprint(_load_script_md(ep)):
            raise ValueError('保存的计划对应旧正文，请先更新计划')
        jobs,_=prepare(ep,plan_path,root)
        jobs=[j for j in jobs if j.id==args.image_key and not getattr(j,'imported',False)]
        if len(jobs)!=1:raise ValueError('请提供当前分镜中唯一的图片编号')
        job=jobs[0]
        job.out_path=paths.episode_dir(args.episode)/'production/v2/images'/job.request_fingerprint()/job.out.name
        result=recover_saved_response(job,args.receipt,budget_path=args.original_budget)
        print(f'已离线恢复图片：{result}；新调用 0 次，仍需检查画面内容与裁切构图。')


if __name__=='__main__':
    main()
