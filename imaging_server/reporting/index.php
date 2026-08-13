<?php
declare(strict_types=1);
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VSTL Reports</title>
  <style>
    body { font: 16px system-ui, sans-serif; max-width: 760px; margin: 48px auto; padding: 0 20px; color: #202124; }
    h1 { margin-bottom: 8px; }
    form { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 28px; }
    label { display: grid; gap: 6px; font-weight: 600; }
    input, select, button { font: inherit; padding: 10px; }
    .wide { grid-column: 1 / -1; }
    button { background: #7030a0; color: white; border: 0; cursor: pointer; }
  </style>
</head>
<body>
  <h1>VSTL Bench Reports</h1>
  <p>Download Restore, QC, Secure Erase, Capture, OS ONLY, or combined audit records.</p>
  <form method="post" action="download.php">
    <label class="wide">Report token
      <input type="password" name="token" required autocomplete="current-password">
    </label>
    <label>Report
      <select name="type">
        <option value="all">All operations</option>
        <option value="restore">Restore</option>
        <option value="qc">QC</option>
        <option value="secure_erase">Secure Erase</option>
        <option value="capture">Capture</option>
        <option value="os_only">OS ONLY</option>
      </select>
    </label>
    <label>Format
      <select name="format">
        <option value="xlsx">Excel XLSX</option>
        <option value="csv">CSV</option>
      </select>
    </label>
    <button class="wide" type="submit">Download report</button>
  </form>
</body>
</html>
