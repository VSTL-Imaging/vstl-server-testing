<?php
declare(strict_types=1);

$tokenFile = '/etc/vstl-report-token';
$expected = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
$provided = (string)($_POST['token'] ?? $_GET['token'] ?? '');
if ($expected === '' || !hash_equals($expected, $provided)) {
    http_response_code(401);
    header('Content-Type: text/plain');
    echo "Unauthorized\n";
    exit;
}
$format = strtolower((string)($_POST['format'] ?? $_GET['format'] ?? 'xlsx'));
$type = strtolower((string)($_POST['type'] ?? $_GET['type'] ?? 'all'));
if (!in_array($format, ['csv', 'xlsx'], true)) {
    $format = 'xlsx';
}
if (!in_array($type, ['all', 'restore', 'qc', 'secure_erase', 'capture'], true)) {
    $type = 'all';
}
$tmp = tempnam('/tmp', 'vstl-report-');
if ($tmp === false) {
    http_response_code(500);
    exit;
}
$exportFormat = ($format === 'csv' && $type === 'all') ? 'csv_zip' : $format;
$extension = $exportFormat === 'csv_zip' ? 'zip' : $format;
$output = $tmp . '.' . $extension;
$importCmd = '/usr/bin/python3 /usr/local/lib/vstl-reporting/import_secure_erase_records.py'
    . ' --records-root ' . escapeshellarg('/images/dev/.vstl-secure-erase')
    . ' --data ' . escapeshellarg('/var/lib/vstl-reports/audits.jsonl');
exec($importCmd, $ignoredImport, $importStatus);
$cmd = '/usr/bin/python3 /usr/local/lib/vstl-reporting/export_reports.py'
    . ' --data ' . escapeshellarg('/var/lib/vstl-reports/audits.jsonl')
    . ' --format ' . escapeshellarg($exportFormat)
    . ' --type ' . escapeshellarg($type)
    . ' --output ' . escapeshellarg($output);
exec($cmd, $ignored, $status);
@unlink($tmp);
if ($status !== 0 || !is_file($output)) {
    http_response_code(500);
    header('Content-Type: text/plain');
    echo "Report generation failed\n";
    exit;
}
$uaeNow = new DateTimeImmutable('now', new DateTimeZone('Asia/Dubai'));
$name = 'vstl-' . str_replace('_', '-', $type) . '-report-' . $uaeNow->format('Ymd-His') . '.' . $extension;
if ($exportFormat === 'csv_zip') {
    header('Content-Type: application/zip');
} elseif ($format === 'csv') {
    header('Content-Type: text/csv; charset=utf-8');
} else {
    header('Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
}
header('Content-Disposition: attachment; filename="' . $name . '"');
header('Content-Length: ' . filesize($output));
readfile($output);
@unlink($output);
