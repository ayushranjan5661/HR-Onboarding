// Renders the "auto-filled from your CIF" read-only block shared by
// the BGV and Document Collection forms.
const PROFILE_LABELS = {
  full_name: "Full Name", email: "Email", contact_number: "Mobile Number",
  alternate_number: "Alternate Contact", date_of_birth: "Date of Birth",
  gender: "Gender", current_address: "Present/Communication Address",
  permanent_address: "Permanent Address", pan_number: "PAN Number",
};

function renderPrefill(profile, targetEl) {
  const rows = Object.entries(PROFILE_LABELS).map(([key, label]) => `
    <div class="field-row">
      <div class="fname">${label}</div>
      <div class="fval">${profile && profile[key] ? profile[key] : "<em style='color:#b0b3b9'>Not provided</em>"}</div>
    </div>`).join("");
  targetEl.innerHTML = rows;
}
