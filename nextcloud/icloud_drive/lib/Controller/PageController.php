<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\Controller;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\IRequest;
use OCP\IUserSession;
use OCP\IGroupManager;

class PageController extends Controller {
    public function __construct(string $appName, IRequest $request, private IUserSession $users, private IGroupManager $groups) {
        parent::__construct($appName, $request);
    }
    #[NoAdminRequired]
    #[NoCSRFRequired]
    public function index(): TemplateResponse {
        $uid = $this->users->getUser()?->getUID() ?? '';
        return new TemplateResponse($this->appName, 'main', ['isAdmin' => $this->groups->isAdmin($uid)]);
    }
}
