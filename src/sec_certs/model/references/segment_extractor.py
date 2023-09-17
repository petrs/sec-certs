from __future__ import annotations

import itertools
import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Literal

import langdetect
import numpy as np
import pandas as pd
import spacy
from rapidfuzz import fuzz

from sec_certs.sample.cc import CCCertificate
from sec_certs.sample.cc_certificate_id import CertificateId
from sec_certs.utils import parallel_processing

nlp = spacy.load("en_core_web_sm")
logger = logging.getLogger(__name__)


def swap_and_filter_dict(dct: dict[str, Any], filter_to_keys: set[str]):
    new_dct: dict[str, set[str]] = {}
    for key, val in dct.items():
        if val in new_dct:
            new_dct[val].add(key)
        else:
            new_dct[val] = {key}

    return {key: frozenset(val) for key, val in new_dct.items() if key in filter_to_keys}


def fill_reference_segments(record: ReferenceRecord) -> ReferenceRecord:
    """
    Open file, read text and extract sentences with `canonical_reference_keyword` match.
    """
    with record.processed_data_source_path.open("r") as handle:
        data = handle.read()

    sentences_with_hits = [
        sent.text for sent in nlp(data).sents if any(x in sent.text for x in record.actual_reference_keywords)
    ]
    if not sentences_with_hits:
        record.segments = None
        return record

    record.segments = set()
    for index, sent in enumerate(sentences_with_hits):
        to_add = ""
        if index > 2:
            to_add += sentences_with_hits[index - 3] + sentences_with_hits[index - 2] + sentences_with_hits[index - 1]

        to_add += sent

        if index < len(sentences_with_hits) - 1:
            to_add += sentences_with_hits[index + 1]

        record.segments.add(to_add)

    if not record.segments:
        record.segments = None

    return record


def preprocess_data_source(record: ReferenceRecord) -> ReferenceRecord:
    # TODO: This shall be reactivate only when we delete the processed data source files after finnishing
    # if record.processed_data_source_path.exists():
    #     return record

    with record.raw_data_source_path.open("r") as handle:
        data = handle.read()

    processed_data = preprocess_txt_func(data, record.actual_reference_keywords)

    with record.processed_data_source_path.open("w") as handle:
        handle.write(processed_data)

    return record


def strip_all(text: str, to_strip) -> str:
    if pd.isna(to_strip):
        return text
    for i in to_strip:
        text = text.replace(i, "")
    return text


def find_bracket_pattern(sentences: set[str], actual_reference_keywords: frozenset[str]):
    patterns = [r"(\[.+?\])(?=.*" + x + r")" for x in actual_reference_keywords]
    res: list[tuple[str, str]] = []

    for sent in sentences:
        for pattern, keyword in zip(patterns, actual_reference_keywords):
            matches = re.findall(pattern, sent, flags=re.MULTILINE | re.UNICODE | re.DOTALL)
            if matches:
                res.append((matches[-1], keyword))
    return res


def preprocess_txt_func(data: str, actual_reference_keywords: frozenset[str]) -> str:
    data = replace_acronyms(data)
    data = replace_citation_identifiers(data, actual_reference_keywords)
    return data


def replace_citation_identifiers(data: str, actual_reference_keywords: frozenset[str]) -> str:
    segments = {sent.text for sent in nlp(data).sents if any([x in sent.text for x in actual_reference_keywords])}
    patterns_to_replace = find_bracket_pattern(segments, actual_reference_keywords)
    for x in patterns_to_replace:
        data = data.replace(x[0], x[1])
    return data


def replace_acronyms(text: str) -> str:
    acronym_replacements = {
        "TOE": "target of evaluation",
        "CC": "certification framework",
        "PP": "protection profile",
        "ST": "security target",
        "SFR": "security Functional Requirement",
        "SFRs": "security Functional Requirements",
        "IC": "integrated circuit",
        "MRTD": "machine readable travel document",
        "TSF": "security functions of target of evaluation",
        "PACE": "password authenticated connection establishment",
    }

    for acronym, replacement in acronym_replacements.items():
        pattern = rf"(?<!\S){re.escape(acronym)}(?!\S)"
        text = re.sub(pattern, replacement, text)

    return text


@dataclass
class ReferenceRecord:
    """
    Data structure to hold objects when extracting text segments from txt files relevant for reference annotations.
    """

    certificate_dgst: str
    raw_data_source_path: Path
    processed_data_source_path: Path
    canonical_reference_keyword: str
    actual_reference_keywords: frozenset[str]
    source: str
    segments: set[str] | None = None

    def to_pandas_tuple(self) -> tuple[str, str, frozenset[str], str, set[str] | None]:
        return (
            self.certificate_dgst,
            self.canonical_reference_keyword,
            self.actual_reference_keywords,
            self.source,
            self.segments,
        )


class ReferenceSegmentExtractor:
    """
    Class to process list of certificates into a dataframe that holds reference segments.
    Should be only called with ReferenceSegmentExtractor()(list_of_certificates)
    """

    def __init__(self):
        pass

    def __call__(self, certs: Iterable[CCCertificate]) -> pd.DataFrame:
        return self._prepare_df_from_cc_dset(certs)

    def _prepare_df_from_cc_dset(self, certs: Iterable[CCCertificate]) -> pd.DataFrame:
        """
        Prepares processed DataFrame for reference annotator training from a list of certificates. This method:
        - Extracts text segments relevant for each reference out of the certificates, forms dataframe from those
        - Loads data splits into train/valid/test (unseen certificates are put into test set)
        - Loads manually annotated samples
        - Combines all of that into single dataframe
        """
        target_certs = [x for x in certs if x.heuristics.st_references.directly_referencing and x.state.st_txt_path]
        report_certs = [
            x for x in certs if x.heuristics.report_references.directly_referencing and x.state.report_txt_path
        ]
        df_targets = self._build_df(target_certs, "target")
        df_reports = self._build_df(report_certs, "report")
        print(f"df_targets shape: {df_targets.shape}")
        print(f"df_reports shape: {df_reports.shape}")
        return ReferenceSegmentExtractor._process_df(pd.concat([df_targets, df_reports]), certs)

    def _build_records(self, certs: list[CCCertificate], source: Literal["target", "report"]) -> list[ReferenceRecord]:
        def get_cert_records(cert: CCCertificate, source: Literal["target", "report"]) -> list[ReferenceRecord]:
            canonical_ref_var = {"target": "st_references", "report": "report_references"}
            actual_ref_var = {"target": "st_keywords", "report": "report_keywords"}
            raw_source_var = {"target": "st_txt_path", "report": "report_txt_path"}

            canonical_references = getattr(cert.heuristics, canonical_ref_var[source]).directly_referencing
            actual_references = getattr(cert.pdf_data, actual_ref_var[source])["cc_cert_id"]
            actual_references = {
                inner_key: CertificateId(outer_key, inner_key).canonical
                for outer_key, val in actual_references.items()
                for inner_key in val
            }
            actual_references = swap_and_filter_dict(actual_references, canonical_references)

            raw_source_dir = getattr(cert.state, raw_source_var[source]).parent
            processed_source_dir = raw_source_dir.parent / "txt_processed"

            return [
                ReferenceRecord(
                    cert.dgst,
                    raw_source_dir / f"{cert.dgst}.txt",
                    processed_source_dir / f"{cert.dgst}.txt",
                    key,
                    val,
                    source,
                )
                for key, val in actual_references.items()
            ]

        (certs[0].state.report_txt_path.parent.parent / "txt_processed").mkdir(exist_ok=True, parents=True)
        (certs[0].state.st_txt_path.parent.parent / "txt_processed").mkdir(exist_ok=True, parents=True)
        return list(itertools.chain.from_iterable(get_cert_records(cert, source) for cert in certs))

    def _build_df(self, certs: list[CCCertificate], source: Literal["target", "report"]) -> pd.DataFrame:
        records = self._build_records(certs, source)
        records = parallel_processing.process_parallel(
            preprocess_data_source,
            records,
            use_threading=False,
            progress_bar=True,
            progress_bar_desc="Preprocessing data",
        )

        results = parallel_processing.process_parallel(
            fill_reference_segments,
            records,
            use_threading=False,
            progress_bar=True,
            progress_bar_desc="Recovering reference segments",
        )
        print(f"I now have {len(results)} in {source} mode")
        return pd.DataFrame.from_records(
            [x.to_pandas_tuple() for x in results],
            columns=["dgst", "canonical_reference_keyword", "actual_reference_keywords", "source", "segments"],
        )

    @staticmethod
    def _get_split_dict() -> dict[str, str]:
        """
        Returns dictionary that maps dgst: split, where split in `train`, `valid`, `test`
        """

        def get_single_dct(pth: Path, split_name: str) -> dict[str, str]:
            with pth.open("r") as handle:
                return dict.fromkeys(json.load(handle), split_name)

        split_directory = Path(str(files("sec_certs.data") / "reference_annotations/split/"))
        return {
            **get_single_dct(split_directory / "train.json", "train"),
            **get_single_dct(split_directory / "valid.json", "valid"),
            **get_single_dct(split_directory / "test.json", "test"),
        }

    @staticmethod
    def _get_annotations_dict() -> dict[tuple[str, str], str]:
        """
        Returns dictionary mapping tuples `(dgst, canonical_reference_keyword) -> label`
        """

        def load_single_df(pth: Path, split_name: str) -> pd.DataFrame:
            return (
                pd.read_csv(pth)
                .assign(label=lambda df_: df_.label.str.replace(" ", "_").str.upper(), split=split_name)
                .replace("NONE", None)
                .dropna(subset="label")
            )

        annotations_directory = Path(str(files("sec_certs.data") / "reference_annotations/manual_annotations/"))
        df_annot = pd.concat(
            [
                load_single_df(annotations_directory / "train.csv", "train"),
                load_single_df(annotations_directory / "valid.csv", "valid"),
                load_single_df(annotations_directory / "test.csv", "test"),
            ]
        )

        return (
            df_annot[["dgst", "canonical_reference_keyword", "label"]]
            .set_index(["dgst", "canonical_reference_keyword"])
            .label.to_dict()
        )

    @staticmethod
    def _process_df(df: pd.DataFrame, certs: Iterable[CCCertificate]) -> pd.DataFrame:
        def process_segment(segment: str, actual_reference_keywords: frozenset[str]) -> str:
            segment = " ".join(segment.split())
            for ref_id in actual_reference_keywords:
                segment = segment.replace(ref_id, "REFERENCED_CERTIFICATE_ID")
            return segment

        """
        Fully processes the dataframe.
        """
        annotations_dict = ReferenceSegmentExtractor._get_annotations_dict()
        split_dct = ReferenceSegmentExtractor._get_split_dict()

        # Retrieve some columns previously lost
        dgst_to_cert_name = {x.dgst: x.name for x in certs}
        cert_id_to_cert_name = {x.heuristics.cert_id: x.name for x in certs}
        dgst_to_extracted_versions = {x.dgst: x.heuristics.extracted_versions for x in certs}
        cert_id_to_extracted_versions = {x.heuristics.cert_id: x.heuristics.extracted_versions for x in certs}

        logger.info(f"Deleting {df.loc[df.segments.isnull()].shape[0]} rows with no segments.")

        df_new = df.copy()
        df_new["full_key"] = df_new.apply(lambda x: (x["dgst"], x["canonical_reference_keyword"]), axis=1)
        to_delete = len(df_new.loc[df_new.segments.isnull()].full_key.unique())
        print(
            f"Deleting records for {to_delete} unique (dgst, referenced_id) pairs, not necessarily labeled ones. These have empty segments."
        )

        df_processed = (
            df.loc[df.segments.notnull()]
            .explode("segments")
            .assign(lang=lambda df_: df_.segments.map(langdetect.detect))
            .loc[lambda df_: df_.lang.isin({"en", "fr", "de"})]
            .groupby(
                ["dgst", "canonical_reference_keyword", "actual_reference_keywords", "source"],
                as_index=False,
                dropna=False,
            )
            .agg({"segments": list, "lang": list})
            .assign(
                split=lambda df_: df_.dgst.map(split_dct),
                label=lambda df_: [
                    annotations_dict.get(x) for x in zip(df_["dgst"], df_["canonical_reference_keyword"])
                ],
                cert_name=lambda df_: df_.dgst.map(dgst_to_cert_name),
                referenced_cert_name=lambda df_: df_.canonical_reference_keyword.map(cert_id_to_cert_name),
                cert_versions=lambda df_: df_.dgst.map(dgst_to_extracted_versions),
                referenced_cert_versions=lambda df_: df_.canonical_reference_keyword.map(cert_id_to_extracted_versions),
                cert_name_stripped_version=lambda df_: df_.apply(
                    lambda x: strip_all(x["cert_name"], x["cert_versions"]), axis=1
                ),
                referenced_cert_name_stripped_version=lambda df_: df_.apply(
                    lambda x: strip_all(x["referenced_cert_name"], x["referenced_cert_versions"]), axis=1
                ),
                name_similarity=lambda df_: df_.apply(
                    lambda x: fuzz.token_set_ratio(
                        x["cert_name_stripped_version"], x["referenced_cert_name_stripped_version"]
                    ),
                    axis=1,
                ),
                name_len_diff=lambda df_: df_.apply(
                    lambda x: np.nan
                    if pd.isnull(x["cert_name_stripped_version"])
                    or pd.isnull(x["referenced_cert_name_stripped_version"])
                    else abs(len(x["cert_name_stripped_version"]) - len(x["referenced_cert_name_stripped_version"])),
                    axis=1,
                ),
            )
            .assign(
                label=lambda df_: df_.label.map(lambda x: x if x is not None else np.nan),
                split=lambda df_: df_.split.map(lambda x: "test" if pd.isnull(x) else x),
            )
        )
        df_processed.segments = df_processed.apply(
            lambda row: [process_segment(x, row.actual_reference_keywords) for x in row.segments], axis=1
        )
        return df_processed
