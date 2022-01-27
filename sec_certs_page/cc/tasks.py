import os
import shutil
from collections import Counter
from datetime import datetime
from operator import itemgetter

import sentry_sdk
from celery.utils.log import get_task_logger
from flask import current_app
from pkg_resources import get_distribution
from sec_certs.dataset.common_criteria import CCDataset

from .. import celery, mongo
from ..common.tasks import make_dataset_paths, process_new_certs, process_removed_certs, process_updated_certs

logger = get_task_logger(__name__)


@celery.task(ignore_result=True)
def notify(run_id):  # pragma: no cover
    # run = mongo.db.cc_log.find_one({"_id": run_id})
    # diffs = mongo.db.cc_diff.find({"run_id": run_id})
    pass


@celery.task(ignore_result=True)
def update_data():  # pragma: no cover
    tool_version = get_distribution("sec-certs").version
    start = datetime.now()
    paths = make_dataset_paths("cc")

    if current_app.config["CC_SKIP_UPDATE"] and paths["output_path"].exists():
        dset = CCDataset.from_json(paths["output_path"])
        dset.root_dir = paths["dset_path"]
        dset.set_local_paths()
    else:
        dset = CCDataset({}, paths["dset_path"], "dataset", "Description")

    if not dset.auxillary_datasets_dir.exists():
        dset.auxillary_datasets_dir.mkdir(parents=True)
    if paths["cve_path"].exists():
        os.symlink(paths["cve_path"], dset.cve_dataset_path)
    if paths["cpe_path"].exists():
        os.symlink(paths["cpe_path"], dset.cpe_dataset_path)

    try:
        with sentry_sdk.start_span(op="cc.all", description="Get full CC dataset"):
            if not current_app.config["CC_SKIP_UPDATE"] or not paths["output_path"].exists():
                with sentry_sdk.start_span(op="cc.get_certs", description="Get certs from web"):
                    dset.get_certs_from_web(update_json=False)
                with sentry_sdk.start_span(op="cc.download_pdfs", description="Download pdfs"):
                    dset.download_all_pdfs(update_json=False)
                with sentry_sdk.start_span(op="cc.convert_pdfs", description="Convert pdfs"):
                    dset.convert_all_pdfs(update_json=False)
                with sentry_sdk.start_span(op="cc.analyze", description="Analyze certificates"):
                    dset.analyze_certificates(update_json=False)
                with sentry_sdk.start_span(op="cc.maintenance_updates", description="Process maintenance updates"):
                    dset.process_maintenance_updates()
                with sentry_sdk.start_span(op="cc.write_json", description="Write JSON"):
                    dset.to_json(paths["output_path"])

            with sentry_sdk.start_span(op="cc.move", description="Move files"):
                for cert in dset:
                    if cert.state.report_pdf_path and cert.state.report_pdf_path.exists():
                        dst = paths["report_pdf"] / f"{cert.dgst}.pdf"
                        if not dst.exists() or dst.stat().st_size < cert.state.report_pdf_path.stat().st_size:
                            cert.state.report_pdf_path.replace(dst)
                    if cert.state.report_txt_path and cert.state.report_txt_path.exists():
                        dst = paths["report_txt"] / f"{cert.dgst}.txt"
                        if not dst.exists() or dst.stat().st_size < cert.state.report_txt_path.stat().st_size:
                            cert.state.report_txt_path.replace(dst)
                    if cert.state.st_pdf_path and cert.state.st_pdf_path.exists():
                        dst = paths["target_pdf"] / f"{cert.dgst}.pdf"
                        if not dst.exists() or dst.stat().st_size < cert.state.st_pdf_path.stat().st_size:
                            cert.state.st_pdf_path.replace(dst)
                    if cert.state.st_txt_path and cert.state.st_txt_path.exists():
                        dst = paths["target_txt"] / f"{cert.dgst}.txt"
                        if not dst.exists() or dst.stat().st_size < cert.state.st_txt_path.stat().st_size:
                            cert.state.st_txt_path.replace(dst)
        old_ids = set(map(itemgetter("_id"), mongo.db.cc.find({}, projection={"_id": 1})))
        current_ids = set(dset.certs.keys())

        new_ids = current_ids.difference(old_ids)
        removed_ids = old_ids.difference(current_ids)
        updated_ids = current_ids.intersection(old_ids)

        cert_states = Counter(key for cert in dset for key in cert.state.to_dict() if cert.state.to_dict()[key])

        end = datetime.now()
        update_result = mongo.db.cc_log.insert_one(
            {
                "start_time": start,
                "end_time": end,
                "tool_version": tool_version,
                "length": len(dset),
                "ok": True,
                "state": dset.state.to_dict(),
                "stats": {
                    "new_certs": len(new_ids),
                    "removed_ids": len(removed_ids),
                    "updated_ids": len(updated_ids),
                    "cert_states": dict(cert_states),
                },
            }
        )
        logger.info(f"Finished run {update_result.inserted_id}.")

        # TODO: Take dataset and certificate state into account when processing into DB.

        with sentry_sdk.start_span(op="cc.db", description="Process certs into DB."):
            process_new_certs("cc", "cc_diff", dset, new_ids, update_result.inserted_id, start)
            process_updated_certs("cc", "cc_diff", dset, updated_ids, update_result.inserted_id, start)
            process_removed_certs("cc", "cc_diff", dset, removed_ids, update_result.inserted_id, start)
        notify.delay(str(update_result.inserted_id))
    except Exception as e:
        end = datetime.now()
        mongo.db.cc_log.insert_one(
            {
                "start_time": start,
                "end_time": end,
                "tool_version": tool_version,
                "length": len(dset),
                "ok": False,
                "error": str(e),
                "state": dset.state.to_dict(),
            }
        )
        raise e
    finally:
        shutil.rmtree(paths["dset_path"], ignore_errors=True)