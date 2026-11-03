"""Pushes on an HR's own token: fields their Zoho role hides, permission errors."""
import pytest

from app.services import zoho_push as zp


def test_hidden_fields_and_uploads_are_left_out():
    payload = {"First_Name": "A", "LinkedIn_Profile_URL": "x"}
    files = [("Photo", "p.jpg", "image/jpeg", b""), ("Resume", "r.pdf", "application/pdf", b"")]
    hidden = zp._drop_hidden({"First_Name", "Photo"}, payload, files)
    assert payload == {"First_Name": "A"}
    assert [f[0] for f in files] == ["Photo"]
    assert hidden == ["LinkedIn_Profile_URL", "Resume"]


def test_all_fields_hidden_refuses():
    with pytest.raises(zp.ZohoPushError):
        zp._drop_hidden({"Other"}, {"First_Name": "A"}, [])


@pytest.mark.parametrize("code,message,expected", [
    (7038, "Permission denied to add record", "not allowed to add records"),
    (7040, "Permission denied to view record(s)", "not allowed to view Zoho record"),
    (7099, "Permission denied to edit record", "Zoho denied your account"),
    (7013, "Field name 'X' is invalid", "Zoho reported an error"),
])
def test_error_messages(code, message, expected):
    response = {"response": {"errors": {"code": code, "message": message}, "status": 1}}
    assert expected in zp._error_message(response, "Form", "1")


def test_table_row_delete_denied_is_recognised():
    denied = {"response": {"errors": {"code": 7338, "message": {
        "CandidateEducation": "Sorry. You are not allowed to delete record in 'Education' tabular section"}},
        "status": 1}}
    assert zp._delete_denied(denied)
    assert not zp._delete_denied({"error": "500 Internal Server Error"})
