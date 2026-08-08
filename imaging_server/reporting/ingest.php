<?php
declare(strict_types=1);

header('Content-Type: application/json');
$tokenFile = '/etc/vstl-report-token';
$expected = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
$provided = (string)($_SERVER['HTTP_X_VSTL_REPORT_TOKEN'] ?? '');
if ($expected === '' || !hash_equals($expected, $provided)) {
    http_response_code(401);
    echo json_encode(['success' => false, 'message' => 'unauthorized']);
    exit;
}
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => 'POST required']);
    exit;
}
$raw = (string)file_get_contents('php://input');
if ($raw === '' || strlen($raw) > 10 * 1024 * 1024) {
    http_response_code(400);
    echo json_encode(['success' => false, 'message' => 'invalid payload size']);
    exit;
}
$payload = json_decode($raw, true);
if (!is_array($payload)) {
    http_response_code(400);
    echo json_encode(['success' => false, 'message' => 'invalid JSON']);
    exit;
}

function vstl_text($value): string {
    if ($value === null) {
        return '';
    }
    if (is_bool($value)) {
        return $value ? 'true' : 'false';
    }
    return trim((string)$value);
}

function vstl_clear_wipe_standard(string $method): string {
    $standards = [
        'NVMe_FORMAT_USER_DATA' => 'NIST SP 800-88 Clear',
        'NVMe_SECURE_DISCARD_CLEAR' => 'NIST SP 800-88 Clear',
        'NVMe_SOFTWARE_ZERO_CLEAR' => 'NIST SP 800-88 Clear',
        'BLKDISCARD' => 'NIST SP 800-88 Clear',
    ];
    return $standards[$method] ?? '';
}

function vstl_clear_only_exception_reason(array $payload): string {
    $text = strtolower(trim(
        vstl_text($payload['brand'] ?? '') . ' ' .
        vstl_text($payload['model'] ?? '') . ' ' .
        vstl_text($payload['model_label'] ?? '') . ' ' .
        vstl_text($payload['product_name'] ?? '')
    ));
    $normalized = preg_replace('/[^a-z0-9]+/', ' ', $text) ?? '';
    $compact = str_replace(' ', '', $normalized);
    $models = [
        [['hp', 'hewlettpackard'], 'elitebook', ['640', 'g10'], 'temporary HP EliteBook 640 G10 clear-only policy'],
        [['hp', 'hewlettpackard'], 'elitebook', ['850', 'g5'], 'temporary HP EliteBook 850 G5 clear-only policy'],
        [['hp', 'hewlettpackard'], 'elitebook', ['850', 'g6'], 'temporary HP EliteBook 850 G6 clear-only policy'],
        [['dell'], 'latitude', ['5520'], 'temporary Dell Latitude 5520 clear-only policy'],
        [['lenovo'], 'x1 carbon', ['gen', '8'], 'temporary Lenovo ThinkPad X1 Carbon 8th Gen clear-only policy'],
        [['lenovo'], 'x1 carbon', ['8th'], 'temporary Lenovo ThinkPad X1 Carbon 8th Gen clear-only policy'],
    ];
    foreach ($models as $rule) {
        $vendorMatched = false;
        foreach ($rule[0] as $vendor) {
            if (preg_match('/\b' . preg_quote($vendor, '/') . '\b/', $normalized) ||
                strpos($compact, $vendor) !== false) {
                $vendorMatched = true;
                break;
            }
        }
        if (!$vendorMatched || strpos($normalized, $rule[1]) === false) {
            continue;
        }
        $tokensMatched = true;
        foreach ($rule[2] as $token) {
            if (!preg_match('/\b' . preg_quote($token, '/') . '\b/', $normalized)) {
                $tokensMatched = false;
                break;
            }
        }
        if ($tokensMatched) {
            return $rule[3];
        }
    }
    return '';
}

function vstl_clear_exception_allowed(array $payload, array $erase): bool {
    $method = vstl_text($erase['method'] ?? $erase['wipe_method'] ?? '');
    return (
        ($erase['clear_only_exception'] ?? false) === true &&
        vstl_clear_wipe_standard($method) !== '' &&
        vstl_clear_only_exception_reason($payload) !== ''
    );
}

function vstl_wipe_standard(string $method, bool $allowClearException = false): string {
    $standards = [
        'NVMe_SANITIZE_BLOCK_ERASE' => 'NIST SP 800-88 Purge',
        'NVMe_SANITIZE_CRYPTO_ERASE' => 'NIST SP 800-88 Purge',
        'NVMe_SANITIZE_OVERWRITE' => 'NIST SP 800-88 Purge',
        'NVMe_FORMAT_CRYPTO' => 'NIST SP 800-88 Purge',
        'ATA_SANITIZE_BLOCK_ERASE' => 'NIST SP 800-88 Purge',
        'ATA_SANITIZE_CRYPTO_SCRAMBLE' => 'NIST SP 800-88 Purge',
        'ATA_SECURITY_ERASE_ENHANCED' => 'NIST SP 800-88 Purge',
        'ATA_SECURITY_ERASE' => 'NIST SP 800-88 Purge',
        'NWIPE_DOD_3PASS' => 'DoD 5220.22-M 3-pass',
    ];
    if (isset($standards[$method])) {
        return $standards[$method];
    }
    if ($allowClearException) {
        return vstl_clear_wipe_standard($method);
    }
    return '';
}

function vstl_is_certifiable_wipe_method(string $method, bool $allowClearException = false): bool {
    return vstl_wipe_standard($method, $allowClearException) !== '';
}

function vstl_sort_recursive($value) {
    if (!is_array($value)) {
        return $value;
    }
    $isList = array_keys($value) === range(0, count($value) - 1);
    foreach ($value as $key => $child) {
        $value[$key] = vstl_sort_recursive($child);
    }
    if (!$isList) {
        ksort($value);
    }
    return $value;
}

function vstl_hash_json(array $value): string {
    return hash(
        'sha256',
        json_encode(vstl_sort_recursive($value), JSON_UNESCAPED_SLASHES)
    );
}

function vstl_date_token(string $value): string {
    if (preg_match('/^(\d{4})-?(\d{2})-?(\d{2})/', $value, $matches)) {
        return $matches[1] . $matches[2] . $matches[3];
    }
    return gmdate('Ymd');
}

function vstl_issue_local_secure_erase_certificate(array &$payload): bool {
    if (!isset($payload['phase3']) || !is_array($payload['phase3'])) {
        return false;
    }
    if (!isset($payload['phase3']['erase']) || !is_array($payload['phase3']['erase'])) {
        return false;
    }
    $erase =& $payload['phase3']['erase'];
    if (($erase['ok'] ?? false) !== true) {
        return false;
    }
    $existing = vstl_text($erase['certificate_id'] ?? '')
        ?: vstl_text($erase['secure_erase_reg_id'] ?? '')
        ?: vstl_text($payload['secure_erase_reg_id'] ?? '');
    if ($existing !== '') {
        return false;
    }

    $method = vstl_text($erase['method'] ?? '');
    $allowClearException = vstl_clear_exception_allowed($payload, $erase);
    $standard = vstl_wipe_standard($method, $allowClearException);
    if (!vstl_is_certifiable_wipe_method($method, $allowClearException)) {
        $erase['wipe_standard'] = 'Unsupported data sanitization method';
        $erase['certificate_status'] = 'refused';
        $erase['certificate_error'] = 'Clear-class and unknown wipe methods are disabled. Only approved purge-class methods can issue a certificate or authorize capture, except temporary model-specific Clear-only exceptions.';
        $erase['capture_gate_recorded'] = false;
        return false;
    }
    $basis = [
        'schema' => 'vstl_secure_erase_report_certificate_v1',
        'serial_no' => vstl_text($payload['serial_no'] ?? ''),
        'mac_id' => vstl_text($payload['mac_id'] ?? ''),
        'bench_id' => vstl_text($payload['bench_id'] ?? ''),
        'brand' => vstl_text($payload['brand'] ?? ''),
        'model' => vstl_text($payload['model'] ?? ''),
        'device' => vstl_text($erase['device'] ?? ''),
        'wipe_method' => $method,
        'wipe_standard' => $standard,
        'duration_sec' => (int)($erase['duration_sec'] ?? 0),
        'session_started_at' => vstl_text($payload['session_started_at'] ?? ''),
        'source' => 'reporting_ingest_backfill',
    ];
    $verificationHash = vstl_hash_json($basis);
    $certificateId = 'SE-' . vstl_date_token($basis['session_started_at'])
        . '-' . strtoupper(substr($verificationHash, 0, 16));
    $certificate = array_merge($basis, [
        'certificate_id' => $certificateId,
        'verification_hash' => $verificationHash,
        'issued_at' => gmdate('c'),
        'issuer' => 'VSTL Reporting Local Certificate',
        'certificate_status' => 'issued',
        'remote_post_ok' => false,
        'remote_error' => 'cloud certificate response was missing; issued by reporting ingest',
    ]);

    $erase['certificate_id'] = $certificateId;
    $erase['secure_erase_reg_id'] = $certificateId;
    $erase['verification_hash'] = $verificationHash;
    $erase['wipe_standard'] = $standard;
    $erase['certificate_status'] = 'issued';
    $erase['remote_post_ok'] = false;
    $erase['certificate'] = $certificate;
    $payload['secure_erase_reg_id'] = $certificateId;
    return true;
}

$localCertificateIssued = vstl_issue_local_secure_erase_certificate($payload);

$record = [
    'received_at' => gmdate('c'),
    'remote_address' => (string)($_SERVER['REMOTE_ADDR'] ?? ''),
    'payload' => $payload,
];
$line = json_encode($record, JSON_UNESCAPED_SLASHES) . PHP_EOL;
$path = '/var/lib/vstl-reports/audits.jsonl';
$handle = fopen($path, 'ab');
if ($handle === false || !flock($handle, LOCK_EX) || fwrite($handle, $line) === false) {
    if (is_resource($handle)) {
        fclose($handle);
    }
    http_response_code(500);
    echo json_encode(['success' => false, 'message' => 'storage failure']);
    exit;
}
fflush($handle);
flock($handle, LOCK_UN);
fclose($handle);
echo json_encode([
    'success' => true,
    'message' => $localCertificateIssued
        ? 'audit stored; local secure-erase certificate issued'
        : 'audit stored',
]);
