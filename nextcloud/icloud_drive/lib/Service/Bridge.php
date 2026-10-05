<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\Service;

use OCP\IConfig;
use OCP\Http\Client\IClientService;
use OCP\Security\ICrypto;

class Bridge {
    public function __construct(private IConfig $config, private IClientService $clients, private ICrypto $crypto) {}
    public function settings(): array {
        return [
            'worker_url' => $this->config->getAppValue('icloud_drive', 'worker_url', 'http://icloud-bridge:8080'),
            'token_configured' => $this->config->getAppValue('icloud_drive', 'worker_token', '') !== '',
        ];
    }
    public function saveSettings(string $url, string $token): void {
        $parts = parse_url($url);
        if (!$parts || !in_array($parts['scheme'] ?? '', ['http', 'https'], true) || empty($parts['host'])
            || isset($parts['user']) || isset($parts['pass']) || isset($parts['query']) || isset($parts['fragment'])) {
            throw new \InvalidArgumentException('Enter a valid internal worker URL without credentials or query parameters.');
        }
        if ($token !== '' && strlen($token) < 32) {
            throw new \InvalidArgumentException('The worker token must contain at least 32 characters.');
        }
        $this->config->setAppValue('icloud_drive', 'worker_url', rtrim($url, '/'));
        if ($token !== '') {
            $this->config->setAppValue('icloud_drive', 'worker_token', 'encrypted:' . $this->crypto->encrypt($token));
        }
    }
    public function request(string $uid, string $method, string $endpoint, array $payload = [], array $query = []): array {
        $settings = $this->settings();
        $token = $this->config->getAppValue('icloud_drive', 'worker_token', '');
        if ($token === '') { return ['status' => 503, 'data' => ['error' => 'The administrator must configure the bridge connection first.']]; }
        if (str_starts_with($token, 'encrypted:')) { $token = $this->crypto->decrypt(substr($token, 10)); }
        $url = rtrim($settings['worker_url'], '/') . '/v1/' . $endpoint;
        if ($query !== []) { $url .= '?' . http_build_query($query); }
        $options = [
            'headers' => ['Authorization' => 'Bearer ' . $token, 'X-Bridge-User' => base64_encode($uid), 'Content-Type' => 'application/json'],
            'timeout' => 65, 'connect_timeout' => 5, 'http_errors' => false,
            // Limited to this administrator-configured service; no global SSRF setting is weakened.
            'nextcloud' => ['allow_local_address' => true],
        ];
        if (in_array($method, ['POST', 'PUT'], true)) { $options['body'] = json_encode($payload, JSON_THROW_ON_ERROR); }
        try {
            $client = $this->clients->newClient();
            $response = match ($method) {
                'GET' => $client->get($url, $options), 'POST' => $client->post($url, $options),
                'PUT' => $client->put($url, $options), 'DELETE' => $client->delete($url, $options),
                default => throw new \InvalidArgumentException('Unsupported method'),
            };
            $data = json_decode((string)$response->getBody(), true, 512, JSON_THROW_ON_ERROR);
            if (!is_array($data)) { throw new \RuntimeException('Unexpected worker response'); }
            if ($endpoint === 'dav' && $response->getStatusCode() === 200) {
                $data['url'] = rtrim($settings['worker_url'], '/') . $data['path'];
            }
            return ['status' => $response->getStatusCode(), 'data' => $data];
        } catch (\Throwable $error) {
            return ['status' => 503, 'data' => ['error' => 'Cannot reach the bridge. Check the worker URL, token and shared Docker network.']];
        }
    }
}
