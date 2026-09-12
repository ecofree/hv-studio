"""Human input approval and machine eligibility for unattended production."""
from pathlib import Path
import time
from app.paths import episode_dir
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint, file_sha256


def inputs(ep_id):
    from app.managers import load
    from app.orchestrator import _load_script_md, build_image_jobs
    from app.services.script_document import parse_document
    folder = episode_dir(ep_id)
    text = _load_script_md(load(ep_id))
    if not text.strip(): raise ValueError('source narration missing')
    files = [folder/'scene_plan.json', folder/'image_prompts.json']
    if any(not p.is_file() for p in files):
        raise ValueError('explicit storyboard and image prompts required before unattended generation')
    jobs = build_image_jobs(ep_id, strict=True)
    if not jobs: raise ValueError('image jobs missing')
    document = parse_document(text)
    unsupported = [c for c in document.get('delivery_cues', []) if c['intent'] != 'pause']
    return dict(source_sha256=fingerprint(text), files={str(p): file_sha256(p) for p in files},
                image_requests={j.id:j.request_fingerprint() for j in jobs},
                unsupported_cues=[c['cue_id'] for c in unsupported])


def record_approval(ep_id, reviewer, finding):
    if not reviewer.strip() or not finding.strip(): raise ValueError('reviewer and findings are required')
    value = inputs(ep_id)
    # Input approval never converts unsupported direction into implemented work.
    path = episode_dir(ep_id)/'production/input-approval.json'
    atomic_json(path, dict(input_fingerprint=fingerprint(value), reviewer=reviewer.strip(),
                           finding=finding.strip(), decision='approved_inputs', reviewed_at=time.time()))
    return path


def check(ep_id):
    blockers = []
    try:
        value = inputs(ep_id)
        path = episode_dir(ep_id)/'production/input-approval.json'
        review = read(path) if path.exists() else {}
        if (review.get('decision') != 'approved_inputs' or not review.get('reviewer')
                or not review.get('finding') or review.get('input_fingerprint') != fingerprint(value)):
            blockers.append('source facts and explicit storyboard require current input approval')
        if value['unsupported_cues']:
            state_path = episode_dir(ep_id)/'production/v2/run.json'
            state = read(state_path) if state_path.exists() else {}
            from app.services.delivery import pending
            if (state.get('source_sha256') != value['source_sha256'] or not state.get('manifest_path')
                    or pending(state['manifest_path'])):
                blockers.append('unsupported artistic directions require a supervised candidate and genuine disposition')
        state_path = episode_dir(ep_id)/'production/v2/run.json'
        if state_path.exists():
            state = read(state_path)
            if state.get('quality_path'):
                from app.services.production_quality import pending
                if pending(state['quality_path']): blockers.append('existing audio findings require disposition')
        return dict(eligible=not blockers, blockers=blockers, input_fingerprint=fingerprint(value))
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        return dict(eligible=False, blockers=[str(exc)], input_fingerprint=None)
