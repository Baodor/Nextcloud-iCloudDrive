<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\Controller;
use OCP\AppFramework\OCSController;
use OCP\AppFramework\Http\DataResponse;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\IRequest;
use OCP\IUserSession;
use OCA\ICloudDrive\Service\Bridge;

class ApiController extends OCSController {
    public function __construct(string $appName, IRequest $request, private IUserSession $users, private Bridge $bridge) {
        parent::__construct($appName, $request);
    }
    private function forward(string $method, string $endpoint, array $payload = []): DataResponse {
        $allow = [
            'GET' => '~^(state|folders|dav)$~D',
            'POST' => '~^(connect/(icloud|nextcloud)|auth/continue|folders|jobs|jobs/[a-f0-9]{32}/run|runs/[a-f0-9]{32}/stop|disconnect/(icloud|nextcloud))$~D',
            'PUT' => '~^jobs/[a-f0-9]{32}$~D',
            'DELETE' => '~^(jobs/[a-f0-9]{32}|account)$~D',
        ];
        if (!preg_match($allow[$method], $endpoint)) { return new DataResponse(['error' => 'Endpoint not found'], 404); }
        $uid = $this->users->getUser()?->getUID();
        if (!$uid) { return new DataResponse(['error' => 'Authentication required'], 401); }
        $query = $endpoint === 'folders' && $method === 'GET'
            ? ['side' => $this->request->getParam('side', 'icloud'), 'path' => $this->request->getParam('path', '')] : [];
        $result = $this->bridge->request($uid, $method, $endpoint, $payload, $query);
        return new DataResponse($result['data'], $result['status']);
    }
    #[NoAdminRequired]
    public function get(string $endpoint): DataResponse { return $this->forward('GET', $endpoint); }
    #[NoAdminRequired]
    public function post(string $endpoint, array $payload = []): DataResponse { return $this->forward('POST', $endpoint, $payload); }
    #[NoAdminRequired]
    public function put(string $endpoint, array $payload = []): DataResponse { return $this->forward('PUT', $endpoint, $payload); }
    #[NoAdminRequired]
    public function delete(string $endpoint): DataResponse { return $this->forward('DELETE', $endpoint); }
}
