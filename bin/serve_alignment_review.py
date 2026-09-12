"""Serve one local review folder with byte-range audio seeking."""
import argparse
from pathlib import Path


def main():
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, Response
    from starlette.routing import Route
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    folder = args.folder.resolve()
    allowed = {'review.html', 'review-audio.wav', 'subtitles.draft.srt'}

    async def serve(request):
        name = request.path_params['name'] or 'review.html'
        if name not in allowed or not (folder / name).is_file():
            return Response(status_code=404)
        return FileResponse(folder / name)

    uvicorn.run(Starlette(routes=[Route('/{name:path}', serve)]),
                host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
