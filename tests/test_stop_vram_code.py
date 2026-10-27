import json
from pathlib import Path

from taskqueue import errors as tqerr
from taskqueue.system_alerts import _STOP_REASON_TO_ERROR_CODES


def test_vram_stop_has_its_own_code() -> None:
    assert tqerr.STOP_VRAM == "tq.stop.vram" != tqerr.STOP_RAM
    assert _STOP_REASON_TO_ERROR_CODES["vram_hard"] == {tqerr.STOP_VRAM}
    labels = json.loads((Path(tqerr.__file__).parent / "static" / "error_labels.json").read_text("utf-8"))
    assert tqerr.STOP_VRAM in labels
