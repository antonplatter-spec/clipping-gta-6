"""Bridge ops for the agent loop (loaded by automate.bridge when an op isn't one of its own).

  dub_validate  {job, pick, lang, translation, n_lines} -> {errors: [str]}
                clipper.dub.validate_translation against the job's picks.json pick (1-based) and its English upload
                entries, exactly what `clipper dub render` checks before any audio work; `n_lines` is the number of
                segments `clipper dub segments` reported (passed in, so no voice separation runs here).
"""
from . import bridge


def dub_validate(payload, cfg):
    from clipper import config, dub, render
    job = config.job_dir(payload["job"])
    meta, pick = dub.load_job(job, int(payload["pick"]))
    upload = dub.english_upload(render, config, pick, meta)
    tr = payload.get("translation")
    if not isinstance(tr, dict):
        raise ValueError("translation must be a JSON object")
    return {"errors": dub.validate_translation(tr, payload.get("lang"), pick, upload, int(payload["n_lines"]))}


bridge.OPS["dub_validate"] = dub_validate
