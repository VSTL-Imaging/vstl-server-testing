<?php
declare(strict_types=1);

header('Content-Type: application/json');

function vstl_reply(int $status, array $body): void {
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_SLASHES);
    exit;
}

$tokenFile = '/etc/vstl-report-token';
$expected = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
$provided = (string)($_SERVER['HTTP_X_VSTL_REPORT_TOKEN'] ?? '');
if ($expected === '' || !hash_equals($expected, $provided)) {
    vstl_reply(401, ['success' => false, 'message' => 'unauthorized']);
}

$resource = strtolower(trim((string)($_GET['resource'] ?? '')));
$benchId = trim((string)($_GET['bench_id'] ?? ''));
if (!in_array($resource, ['session', 'queue'], true) || $benchId === '' || strlen($benchId) > 128) {
    vstl_reply(400, ['success' => false, 'message' => 'invalid resource or bench_id']);
}

$method = strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET'));
$dataRoot = '/var/lib/vstl-reports/bench-state';
$benchDir = $dataRoot . '/' . hash('sha256', $benchId);
$sessionPath = $benchDir . '/session.json';
$queueDir = $benchDir . '/queue';

function vstl_json_body(int $limit): array {
    $raw = (string)file_get_contents('php://input');
    if ($raw === '' || strlen($raw) > $limit) {
        vstl_reply(400, ['success' => false, 'message' => 'invalid payload size']);
    }
    $body = json_decode($raw, true);
    if (!is_array($body)) {
        vstl_reply(400, ['success' => false, 'message' => 'invalid JSON']);
    }
    return $body;
}

function vstl_atomic_json_write(string $path, array $value): bool {
    $directory = dirname($path);
    if (!is_dir($directory) && !mkdir($directory, 0750, true) && !is_dir($directory)) {
        return false;
    }
    $temporary = tempnam($directory, '.tmp-');
    if ($temporary === false) {
        return false;
    }
    $encoded = json_encode($value, JSON_UNESCAPED_SLASHES);
    $ok = $encoded !== false
        && file_put_contents($temporary, $encoded, LOCK_EX) !== false
        && chmod($temporary, 0640)
        && rename($temporary, $path);
    if (!$ok && is_file($temporary)) {
        @unlink($temporary);
    }
    return $ok;
}

if ($resource === 'session') {
    if ($method === 'GET') {
        if (!is_readable($sessionPath)) {
            vstl_reply(200, ['success' => true, 'session' => null]);
        }
        $session = json_decode((string)file_get_contents($sessionPath), true);
        vstl_reply(200, [
            'success' => true,
            'session' => is_array($session) ? $session : null,
        ]);
    }
    if ($method === 'POST') {
        $body = vstl_json_body(64 * 1024);
        $session = $body['session'] ?? null;
        if (!is_array($session) || trim((string)($session['token'] ?? '')) === '') {
            vstl_reply(400, ['success' => false, 'message' => 'session token required']);
        }
        $stored = [
            'token' => (string)$session['token'],
            'expires_at' => (string)($session['expires_at'] ?? ''),
            'session_id' => (string)($session['session_id'] ?? ''),
            'selected_layer' => (string)($session['selected_layer'] ?? ''),
            'user' => is_array($session['user'] ?? null) ? $session['user'] : [],
            'cached_at' => gmdate('c'),
        ];
        if (!vstl_atomic_json_write($sessionPath, $stored)) {
            vstl_reply(500, ['success' => false, 'message' => 'session storage failure']);
        }
        vstl_reply(200, ['success' => true]);
    }
    if ($method === 'DELETE') {
        if (is_file($sessionPath) && !@unlink($sessionPath)) {
            vstl_reply(500, ['success' => false, 'message' => 'session delete failure']);
        }
        vstl_reply(200, ['success' => true]);
    }
    vstl_reply(405, ['success' => false, 'message' => 'method not allowed']);
}

if ($method === 'POST') {
    $body = vstl_json_body(12 * 1024 * 1024);
    $envelope = $body['envelope'] ?? null;
    $submissionId = trim((string)($envelope['submission_id'] ?? ''));
    if (!is_array($envelope) || $submissionId === '' || !is_array($envelope['payload'] ?? null)) {
        vstl_reply(400, ['success' => false, 'message' => 'invalid queue envelope']);
    }
    $path = $queueDir . '/' . hash('sha256', $submissionId) . '.json';
    if (is_file($path)) {
        vstl_reply(200, ['success' => true, 'queued' => false, 'already_exists' => true]);
    }
    if (!vstl_atomic_json_write($path, $envelope)) {
        vstl_reply(500, ['success' => false, 'message' => 'queue storage failure']);
    }
    vstl_reply(200, ['success' => true, 'queued' => true]);
}

if ($method === 'GET') {
    $operatorUserId = trim((string)($_GET['operator_user_id'] ?? ''));
    $items = [];
    if (is_dir($queueDir)) {
        $paths = glob($queueDir . '/*.json') ?: [];
        sort($paths, SORT_STRING);
        foreach (array_slice($paths, 0, 250) as $path) {
            $item = json_decode((string)file_get_contents($path), true);
            if (!is_array($item)) {
                continue;
            }
            if ($operatorUserId !== '' && (string)($item['operator_user_id'] ?? '') !== $operatorUserId) {
                continue;
            }
            $items[] = $item;
        }
    }
    vstl_reply(200, ['success' => true, 'items' => $items]);
}

if ($method === 'DELETE') {
    $submissionId = trim((string)($_GET['submission_id'] ?? ''));
    if ($submissionId === '') {
        vstl_reply(400, ['success' => false, 'message' => 'submission_id required']);
    }
    $path = $queueDir . '/' . hash('sha256', $submissionId) . '.json';
    if (is_file($path) && !@unlink($path)) {
        vstl_reply(500, ['success' => false, 'message' => 'queue delete failure']);
    }
    vstl_reply(200, ['success' => true]);
}

vstl_reply(405, ['success' => false, 'message' => 'method not allowed']);
