<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\Controller;
use OCP\AppFramework\OCSController;
use OCP\AppFramework\Http\DataResponse;
use OCP\IRequest;
use OCA\ICloudDrive\Service\Bridge;

class AdminController extends OCSController {
    public function __construct(string $appName, IRequest $request, private Bridge $bridge) { parent::__construct($appName, $request); }
    public function get(): DataResponse { return new DataResponse($this->bridge->settings()); }
    public function save(array $payload = []): DataResponse {
        try {
            $this->bridge->saveSettings((string)($payload['worker_url'] ?? ''), (string)($payload['worker_token'] ?? ''));
            return new DataResponse($this->bridge->settings());
        } catch (\InvalidArgumentException $error) {
            return new DataResponse(['error' => $error->getMessage()], 400);
        }
    }
}
