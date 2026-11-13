"""Explicit final review action. No automatic approvals or media generation."""
from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import file_sha256


def confirm(eid, expected_receipt, reviewer, finding):
    from app.services.production_status import get_status
    from app.services.narration_pipeline import record_listening_review
    from app.services.production_v2 import review_candidate,publish
    from app.services.process_lock import file_lock
    from app.orchestrator import _episode_lock
    root=paths.episode_dir(eid)/'production/v2'
    with _episode_lock(eid), file_lock(root/'studio-confirm.lock'):
        state=read(root/'run.json');receipt=state['receipt_path']
        if file_sha256(receipt)!=expected_receipt:raise ValueError('成片已变化，请重新载入观看')
        status=get_status(eid)
        if not status.get('candidate_path'):raise ValueError('当前候选无效')
        if ('stale_or_invalid' in status.get('dimensions',{}).values() or
                status.get('alignment',{}).get('status')=='stale_or_invalid'):
            raise ValueError('音频质检或字幕时序证据已变化，请重新检查后再确认导出')
        if any(status.get(k) for k in ('quality_pending_units','delivery_pending','alignment_review_units')):
            raise ValueError('请先处理读音、导演提示或切片边界的具体问题')
        if not reviewer.strip() or not finding.strip():raise ValueError('请填写确认人和实际结论')
        audio=record_listening_review(state['manifest_path'],reviewer,finding,'approved')
        visual=review_candidate(receipt,reviewer,finding,'approved')
        return publish(receipt,visual,audio,root/'published.json')
