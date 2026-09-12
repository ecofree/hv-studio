"""Explicit v2 lifecycle under the shared CLI."""
from pathlib import Path
from app.services import narration_pipeline as N
from app.services import production_v2 as P


def add_cli(subparsers):
    parser=subparsers.add_parser("narration",help="v2: source → immutable audio → candidate → reviewed publication")
    sub=parser.add_subparsers(dest="narration_action",required=True)
    p=sub.add_parser("prepare")
    p.add_argument("source",type=Path);p.add_argument("destination",type=Path)
    p.add_argument("--max-units",type=int)
    p.set_defaults(func=lambda a:print(N.prepare(a.source,a.destination,a.max_units)))
    p=sub.add_parser("revise-delivery")
    p.add_argument("plan",type=Path);p.add_argument("unit_id")
    p.add_argument("--rate",required=True,type=float)
    p.set_defaults(func=lambda a:print(N.revise_delivery(a.plan,a.unit_id,a.rate)))
    p=sub.add_parser("import-voice")
    p.add_argument("cache",type=Path);p.add_argument("destination",type=Path)
    p.set_defaults(func=lambda a:(N.import_voice(a.cache,a.destination),print(a.destination)))
    p=sub.add_parser("synthesize")
    p.add_argument("plan",type=Path);p.add_argument("voice_asset",type=Path)
    p.add_argument("--max-requests",required=True,type=int)
    p.add_argument("--max-characters",required=True,type=int)
    p.add_argument("--force",action="store_true")
    p.set_defaults(func=lambda a:print(N.synthesize(a.plan,a.voice_asset,max_requests=a.max_requests,
        max_characters=a.max_characters,force=a.force)))
    p=sub.add_parser("render")
    p.add_argument("manifest",type=Path);p.add_argument("timeline",type=Path);p.add_argument("destination",type=Path)
    p.add_argument("--timeout",type=int,default=300)
    p.set_defaults(func=lambda a:print(P.render(a.manifest,a.timeline,a.destination,a.timeout)))
    from app.services import subtitle_alignment as A
    from app.services import production_quality as Q, delivery as D
    for name, fn, choices in [('review-quality', Q.record_disposition, ['asr_false_positive','spoken_error']),
                               ('review-direction', D.record_review, ['accepted_existing','needs_revision'])]:
        p=sub.add_parser(name)
        p.add_argument('artifact',type=Path);p.add_argument('item_id')
        p.add_argument('--reviewer',required=True);p.add_argument('--findings',required=True)
        p.add_argument('--decision',choices=choices,required=True)
        p.set_defaults(func=lambda a,fn=fn:print(fn(a.artifact,a.item_id,a.reviewer,a.findings,a.decision)))
    p=sub.add_parser("align-draft",help="本地 ASR 词边界生成待审核字幕，不调用云端")
    p.add_argument("manifest",type=Path);p.add_argument("destination",type=Path)
    p.set_defaults(func=lambda a:print(A.build(a.manifest,a.destination)))
    for name,fn in (("review-audio",N.record_listening_review),("review-video",P.review_candidate),("review-alignment",A.review)):
        p=sub.add_parser(name)
        p.add_argument("artifact",type=Path);p.add_argument("--reviewer",required=True)
        p.add_argument("--findings",required=True);p.add_argument("--decision",choices=["approved","rejected"],required=True)
        p.set_defaults(func=lambda a,fn=fn:print(fn(a.artifact,a.reviewer,a.findings,a.decision)))
    p=sub.add_parser("publish")
    p.add_argument("receipt",type=Path);p.add_argument("video_review",type=Path)
    p.add_argument("audio_review",type=Path);p.add_argument("pointer",type=Path)
    p.set_defaults(func=lambda a:print(P.publish(a.receipt,a.video_review,a.audio_review,a.pointer)))
