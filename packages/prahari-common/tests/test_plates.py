"""plates.py is the shared vocabulary inference and the match engine must
agree on byte-for-byte -- a divergence here is the exact silent failure the
whole match-engine design exists to prevent (see the module docstring). These
tests pin normalisation, format classification, masking, and confidence
re-alignment against real Gujarat-style plate shapes.
"""

from __future__ import annotations

from prahari_common.plates import (
    MASK_ALPHA,
    MASK_DIGIT,
    MASK_FREE,
    PlateFormat,
    normalise_plate,
    project_confidences,
)


class TestNormalisation:
    def test_uppercases(self) -> None:
        assert normalise_plate("gj01ab1234").text == "GJ01AB1234"

    def test_strips_separators(self) -> None:
        assert normalise_plate("GJ-01-AB-1234").text == "GJ01AB1234"
        assert normalise_plate("GJ 01 AB 1234").text == "GJ01AB1234"

    def test_strips_ind_prefix(self) -> None:
        assert normalise_plate("IND GJ01AB1234").text == "GJ01AB1234"
        assert normalise_plate("INDGJ01AB1234").text == "GJ01AB1234"

    def test_short_ind_like_string_is_not_stripped_to_empty(self) -> None:
        # A 3-character read that happens to BE "IND" must not vanish -- that
        # would make it normalise to "" and match everything in stage 2 of
        # the funnel.
        result = normalise_plate("IND")
        assert result.text == "IND"

    def test_never_raises_on_garbage_input(self) -> None:
        # Unparseable is a FORMAT (NONCONFORMING), never an exception --
        # otherwise one bad OCR read would crash the pipeline.
        result = normalise_plate("!!!###???")
        assert result.format == PlateFormat.NONCONFORMING

    def test_never_corrects_a_character(self) -> None:
        # The invariant events.proto states explicitly: normalisation must
        # not turn a confusable character into what the format "wants". That
        # is a scored, auditable match-time decision, not a silent parse-time
        # one.
        result = normalise_plate("GJ01AB1Z34")
        assert result.text == "GJ01AB1Z34"  # the "Z" survives uncorrected


class TestFormatClassification:
    def test_standard_format(self) -> None:
        result = normalise_plate("GJ01AB1234")
        assert result.format == PlateFormat.STANDARD
        assert result.mask == MASK_ALPHA * 2 + MASK_DIGIT * 2 + MASK_ALPHA * 2 + MASK_DIGIT * 4
        assert result.is_parsed

    def test_standard_format_with_no_series_letters(self) -> None:
        # Early Gujarat registrations have no series letters at all -- the
        # series group is genuinely optional, not a fixed two characters.
        result = normalise_plate("GJ011234")
        assert result.format == PlateFormat.STANDARD
        assert result.mask == MASK_ALPHA * 2 + MASK_DIGIT * 2 + MASK_DIGIT * 4

    def test_standard_format_with_single_digit_district(self) -> None:
        result = normalise_plate("GJ1AB1234")
        assert result.format == PlateFormat.STANDARD

    def test_bh_series_format(self) -> None:
        result = normalise_plate("23BH1234AB")
        assert result.format == PlateFormat.BH_SERIES
        assert result.mask == MASK_DIGIT * 2 + MASK_ALPHA * 2 + MASK_DIGIT * 4 + MASK_ALPHA * 2

    def test_military_format(self) -> None:
        result = normalise_plate("12A123456B")
        assert result.format == PlateFormat.MILITARY

    def test_nonconforming_format_is_kept_not_discarded(self) -> None:
        # A plate we cannot classify is still evidence a vehicle passed a
        # camera -- dropping it would make route reconstruction lie by
        # omission.
        result = normalise_plate("TRAC7788")
        assert result.format == PlateFormat.NONCONFORMING
        assert result.mask == MASK_FREE * len("TRAC7788")
        assert not result.is_parsed

    def test_unspecified_is_not_considered_parsed(self) -> None:
        assert not normalise_plate("").is_parsed


class TestSourceIndexAlignment:
    def test_source_index_tracks_position_in_the_raw_string(self) -> None:
        # Confidence is aligned to the RAW string; stripped separators shift
        # every subsequent character's position, so `source_index` must record
        # where each surviving character actually came from.
        result = normalise_plate("GJ-01-AB-1234")
        raw = "GJ-01-AB-1234"
        for text_pos, raw_pos in enumerate(result.source_index):
            assert raw[raw_pos].upper() == result.text[text_pos]

    def test_source_index_shifts_after_ind_prefix_is_stripped(self) -> None:
        result = normalise_plate("INDGJ01AB1234")
        raw = "INDGJ01AB1234"
        for text_pos, raw_pos in enumerate(result.source_index):
            assert raw[raw_pos].upper() == result.text[text_pos]


class TestProjectConfidences:
    def test_confidences_realign_after_separators_are_stripped(self) -> None:
        plate = normalise_plate("GJ-01-AB-1234")
        # One confidence value per RAW character, including the separators.
        raw_confidences = [1.0 - i * 0.01 for i in range(len("GJ-01-AB-1234"))]
        projected = project_confidences(raw_confidences, plate)
        assert len(projected) == len(plate.text)
        for text_pos, raw_pos in zip(range(len(plate.text)), plate.source_index, strict=True):
            assert projected[text_pos] == raw_confidences[raw_pos]

    def test_missing_confidence_defaults_to_least_trusted(self) -> None:
        plate = normalise_plate("GJ01AB1234")
        # No confidences supplied at all.
        projected = project_confidences([], plate)
        assert projected == (0.0,) * len(plate.text)

    def test_default_is_configurable(self) -> None:
        plate = normalise_plate("GJ01AB1234")
        projected = project_confidences([], plate, default=0.5)
        assert projected == (0.5,) * len(plate.text)

    def test_short_confidence_list_defaults_the_missing_tail(self) -> None:
        plate = normalise_plate("GJ01AB1234")
        projected = project_confidences([0.9, 0.8], plate)
        assert projected[0] == 0.9
        assert projected[1] == 0.8
        assert all(c == 0.0 for c in projected[2:])
