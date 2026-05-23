/**
 * apps_script_v2.gs
 * ==================
 * Backward-compatible Apps Script for sending queued emails.
 *
 * Works with the v3 schema (with html_body, from_account, idempotency_key,
 * attempt_count, error_message columns) but gracefully handles old rows
 * that don't have these columns.
 *
 * Key changes from v1:
 *   - Reads columns by HEADER NAME, not column letter (resilient to schema changes)
 *   - Sends HTML body when html_body column has content; falls back to plain
 *   - Writes ISO timestamps for interop with Python (audit error 3.8)
 *   - Increments attempt_count on each try
 *   - Writes error_message on failure for visibility in Stage 4 queue view
 *   - Skip rows already Sent (idempotent)
 *
 * INSTALLATION:
 *   1. Open the spreadsheet in Google Sheets
 *   2. Extensions → Apps Script
 *   3. Replace the existing script content with this file
 *   4. Save
 *   5. Run sendQueuedEmails() once manually to authorize
 *   6. Optional: install a time-based trigger (every 5 minutes) for automation
 *
 * IMPORTANT: this script preserves the previous sendQueuedEmails() function
 * name so any existing triggers continue to work.
 */

// ============================================================================
// CONFIG
// ============================================================================

var EMAILS_TAB_NAME = "Emails";
var MAX_SENDS_PER_RUN = 15;  // rate limit safety (~ 100/day if run every 5 min)
var MAX_ATTEMPTS = 3;        // give up after this many failures


// ============================================================================
// MAIN ENTRY (preserved name for trigger compatibility)
// ============================================================================

function sendQueuedEmails() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(EMAILS_TAB_NAME);

  if (!sheet) {
    Logger.log("ERROR: Could not find " + EMAILS_TAB_NAME + " tab.");
    return;
  }

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) {
    Logger.log("No data rows to process.");
    return;
  }

  var headers = data[0];
  var headerMap = buildHeaderMap(headers);

  // Required columns
  if (!validateRequiredColumns(headerMap)) {
    return;
  }

  var sentThisRun = 0;
  var skippedThisRun = 0;
  var failedThisRun = 0;

  for (var i = 1; i < data.length; i++) {
    if (sentThisRun >= MAX_SENDS_PER_RUN) {
      Logger.log("Hit max sends per run (" + MAX_SENDS_PER_RUN + "). Stopping.");
      break;
    }

    var row = data[i];
    var rowNum = i + 1; // 1-indexed for getRange

    var status = String(getCell(row, headerMap, "status") || "").toLowerCase();

    // Skip non-Queued rows
    if (status !== "queued") {
      skippedThisRun++;
      continue;
    }

    var attemptCount = parseInt(getCell(row, headerMap, "attempt_count") || "0", 10);
    if (attemptCount >= MAX_ATTEMPTS) {
      Logger.log("Row " + rowNum + " exceeded max attempts (" + MAX_ATTEMPTS + "). Marking Bounced.");
      updateCells(sheet, rowNum, headerMap, {
        status: "Bounced",
        error_message: "Exceeded max " + MAX_ATTEMPTS + " attempts",
        last_attempt_at: nowIso(),
      });
      failedThisRun++;
      continue;
    }

    // Attempt send
    var result = attemptSend(row, headerMap);

    if (result.success) {
      updateCells(sheet, rowNum, headerMap, {
        status: "Sent",
        sent_at: nowIso(),
        last_attempt_at: nowIso(),
        attempt_count: attemptCount + 1,
        error_message: "",
      });
      sentThisRun++;
      Logger.log("✓ Sent row " + rowNum + " to " + result.recipient);
    } else {
      updateCells(sheet, rowNum, headerMap, {
        status: "Failed",
        last_attempt_at: nowIso(),
        attempt_count: attemptCount + 1,
        error_message: result.error,
      });
      failedThisRun++;
      Logger.log("✗ Failed row " + rowNum + " to " + result.recipient + ": " + result.error);
    }
  }

  Logger.log(
    "Run complete. Sent: " + sentThisRun +
    ", Failed: " + failedThisRun +
    ", Skipped: " + skippedThisRun
  );
}


// ============================================================================
// SEND ONE EMAIL
// ============================================================================

function attemptSend(row, headerMap) {
  var recipient = getCell(row, headerMap, "recipient_email") || "";
  var subject = getCell(row, headerMap, "subject") || "";
  var plainBody = getCell(row, headerMap, "body") || "";
  var htmlBody = getCell(row, headerMap, "html_body") || "";
  var fromAccount = getCell(row, headerMap, "from_account") || "";

  // Basic validation
  if (!recipient) {
    return { success: false, error: "Missing recipient_email", recipient: "" };
  }
  if (!subject) {
    return { success: false, error: "Missing subject", recipient: recipient };
  }
  if (!plainBody) {
    return { success: false, error: "Missing body", recipient: recipient };
  }

  // Build send options
  var options = {};

  // HTML body — only include if non-empty
  if (htmlBody && htmlBody.trim().length > 0) {
    options.htmlBody = htmlBody;
  }

  // Sender — use 'from' if specified and we have permission
  // Note: GmailApp.sendEmail's 'from' requires the account to be an alias
  // of the authenticated user. For now, just send from the script's owner.
  if (fromAccount && fromAccount.trim().length > 0) {
    var aliases = GmailApp.getAliases();
    if (aliases.indexOf(fromAccount.trim()) !== -1) {
      options.from = fromAccount.trim();
    }
    // If not an alias, silently fall back to default — don't fail the send.
  }

  try {
    GmailApp.sendEmail(recipient, subject, plainBody, options);
    return { success: true, recipient: recipient };
  } catch (e) {
    return {
      success: false,
      error: String(e.message || e).substring(0, 500),
      recipient: recipient,
    };
  }
}


// ============================================================================
// HELPERS
// ============================================================================

function buildHeaderMap(headers) {
  /**
   * Convert headers array into a name → column-index lookup.
   * Audit error 3.11: read by name, not letter.
   */
  var map = {};
  for (var i = 0; i < headers.length; i++) {
    var h = String(headers[i] || "").trim();
    if (h) {
      map[h] = i;
    }
  }
  return map;
}


function validateRequiredColumns(headerMap) {
  /** Ensure the schema has the columns we need. */
  var required = ["status", "recipient_email", "subject", "body"];
  var missing = [];
  for (var i = 0; i < required.length; i++) {
    if (typeof headerMap[required[i]] === "undefined") {
      missing.push(required[i]);
    }
  }
  if (missing.length > 0) {
    Logger.log("ERROR: Emails tab missing required columns: " + missing.join(", "));
    return false;
  }
  return true;
}


function getCell(row, headerMap, columnName) {
  /** Safe lookup — returns undefined if column missing, not an error. */
  var idx = headerMap[columnName];
  if (typeof idx === "undefined") return undefined;
  return row[idx];
}


function updateCells(sheet, rowNum, headerMap, updates) {
  /**
   * Update multiple cells in a row by column name.
   * Skips columns that don't exist on the sheet.
   */
  for (var colName in updates) {
    var colIdx = headerMap[colName];
    if (typeof colIdx === "undefined") continue;
    var colNum = colIdx + 1; // 1-indexed for getRange
    sheet.getRange(rowNum, colNum).setValue(updates[colName]);
  }
}


function nowIso() {
  /**
   * ISO 8601 UTC timestamp — matches Python's now_iso() format.
   * Audit error 3.8: consistent format across Python ↔ Apps Script.
   */
  return Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'");
}


// ============================================================================
// SETUP HELPERS (run manually once if needed)
// ============================================================================

function installFiveMinuteTrigger() {
  /**
   * Install a time-based trigger that runs sendQueuedEmails() every 5 min.
   * Run this once from the Apps Script editor.
   */
  // Remove existing triggers for this function (avoid duplicates)
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "sendQueuedEmails") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }

  ScriptApp.newTrigger("sendQueuedEmails")
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log("Installed 5-minute trigger for sendQueuedEmails.");
}


function resetFailedRowsToQueued() {
  /**
   * Manually reset all Failed rows back to Queued (clears retry counter).
   * Use this if you've fixed the underlying issue and want to retry the batch.
   */
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(EMAILS_TAB_NAME);
  var data = sheet.getDataRange().getValues();
  var headers = data[0];
  var headerMap = buildHeaderMap(headers);

  var resetCount = 0;
  for (var i = 1; i < data.length; i++) {
    var status = String(getCell(data[i], headerMap, "status") || "").toLowerCase();
    if (status === "failed") {
      updateCells(sheet, i + 1, headerMap, {
        status: "Queued",
        attempt_count: 0,
        error_message: "",
        last_attempt_at: "",
      });
      resetCount++;
    }
  }
  Logger.log("Reset " + resetCount + " Failed rows to Queued.");
}
