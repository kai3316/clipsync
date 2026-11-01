"""The phone's upload batch survives a failure instead of dropping the rest.

`startUpload` sends one file per request and walks a queue; the queue used to be
abandoned on the first bad answer, so picking ten photos and having the third one
rejected meant the last seven were silently never sent.  U140 asked for per-file
results plus a queue that continues, and U174's "retry" was folded into it — one
piece of logic, so one file of tests.

Text-level checks again, for the reason the other mobile tests give: the page is
vanilla JS inside one 7 000-line HTML file.  What is verifiable here is the shape
of the control flow — a failure branch that keeps walking the queue, an end of
batch that says what happened, and a retry that holds real `File` objects.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def upload_body() -> str:
    """The `startUpload` function, from its signature to its closing brace."""
    text = source()
    start = text.index("function startUpload(files) {")
    # The function ends with the queue kick-off; everything after it is the
    # file input's own handler.
    end = text.index("fileInput.addEventListener('change'", start)
    return text[start:end]


def test_one_bad_answer_does_not_throw_away_the_queue():
    body = upload_body()
    load = re.search(r"xhr\.onload = function \(\) \{.*?\n      \};", body, re.S)
    assert load, "the response handler is gone"
    block = load.group(0)
    # The failure path records the file and moves on — no reset, no early exit.
    assert "failed.push(file)" in block
    assert "uploadNext();" in block
    assert "resetUploadUI()" not in block, "a failed file still aborts the batch"

    error = re.search(r"xhr\.onerror = function \(\) \{.*?\n      \};", body, re.S)
    assert error, "the transport-error handler is gone"
    assert "uploadNext();" in error.group(0)
    assert "resetUploadUI()" not in error.group(0)


def test_the_bytes_of_a_failed_file_still_count_towards_the_bar():
    """Otherwise the bar never reaches the end of a batch that ended badly.

    A file that came back 413 is done being sent: the reader is not waiting for
    it any more, and a bar stuck at 70% reads as "still going".
    """
    load = re.search(r"xhr\.onload = function \(\) \{.*?\n      \};", upload_body(), re.S)
    assert load
    block = load.group(0)
    assert "uploadedBytes += file.size;" in block
    assert block.index("uploadedBytes += file.size;") < block.index("failed.push(file)")


def test_the_batch_ends_with_a_summary_that_names_the_failures():
    body = upload_body()
    end = re.search(r"if \(queue\.length === 0\) \{.*?\n      \}", body, re.S)
    assert end, "the end-of-batch branch is gone"
    block = end.group(0)
    assert "if (failed.length === 0) showToast(T.uploadOk);" in block
    assert "showUploadResult(doneCount, failed);" in block
    # Files that did land have to show up in the list behind the button.
    assert "loadFiles();" in block

    summary = re.search(r"function uploadSummary\(okCount, failed\) \{.*?\n  \}", source(), re.S)
    assert summary, "the summary sentence is gone"
    text = summary.group(0)
    assert "failed[i].name" in text
    # Three names is a sentence; ten is a paragraph nobody reads before the
    # retry button, so the tail becomes a count.
    assert "i < 3" in text
    assert "T.uploadMore.replace('{n}'" in text
    assert "T.uploadNameSep" in text


def test_the_outcome_row_offers_retry_and_dismissal():
    text = source()
    row = re.search(r'<div class="upload-result" id="uploadResult".*?</div>\s*</div>', text, re.S)
    assert row, "the outcome row is gone"
    block = row.group(0)
    assert 'id="uploadResultText"' in block
    assert 'id="uploadRetryBtn"' in block
    assert 'id="uploadDismissBtn"' in block
    # Announced, not just drawn: this is the only account of a partly failed
    # batch that outlives the toast.
    assert 'role="status"' in block

    # And it holds the files themselves — a retry has to append bytes, and a
    # `{name: …}` would append "[object Object]".
    show = re.search(r"function showUploadResult\(okCount, failed\) \{.*?\n  \}", text, re.S)
    assert show, "showUploadResult is gone"
    assert "uploadFailed = failed.slice();" in show.group(0)
    assert "failed.push(file)" in upload_body()
    assert "failed.push({ name:" not in upload_body()


def test_retry_resends_exactly_what_failed():
    text = source()
    handler = re.search(r"uploadRetryBtn\.addEventListener\('click', function \(\) \{.*?\}\);", text, re.S)
    assert handler, "the retry button is not wired"
    block = handler.group(0)
    # Copied before `startUpload` runs: it clears the list as it starts.
    assert "var again = uploadFailed.slice();" in block
    assert "startUpload(again)" in block
    assert "uploadDismissBtn.addEventListener('click', hideUploadResult)" in text


def test_cancelling_still_says_what_already_landed():
    """Stopping the batch is not the same as nothing having happened."""
    body = upload_body()
    abort = re.search(r"xhr\.onabort = function \(\) \{.*?\n      \};", body, re.S)
    assert abort, "the cancel handler is gone"
    block = abort.group(0)
    assert "cancelled = true;" in block
    assert "if (doneCount > 0) loadFiles();" in block
    assert "T.uploadCancelledSome.replace('{n}'" in block
