<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\AppInfo;

use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IRegistrationContext;
use OCP\User\Events\BeforeUserDeletedEvent;
use OCA\ICloudDrive\Listener\UserDeletedListener;

class Application extends App implements IBootstrap {
    public const APP_ID = 'icloud_drive';
    public function __construct() { parent::__construct(self::APP_ID); }
    public function register(IRegistrationContext $context): void {
        $context->registerEventListener(BeforeUserDeletedEvent::class, UserDeletedListener::class);
    }
    public function boot(IBootContext $context): void {}
}
