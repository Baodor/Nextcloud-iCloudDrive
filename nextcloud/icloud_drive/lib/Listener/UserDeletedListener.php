<?php
declare(strict_types=1);
namespace OCA\ICloudDrive\Listener;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\User\Events\BeforeUserDeletedEvent;
use OCA\ICloudDrive\Service\Bridge;
use Psr\Log\LoggerInterface;

/** @implements IEventListener<BeforeUserDeletedEvent> */
class UserDeletedListener implements IEventListener {
    public function __construct(private Bridge $bridge, private LoggerInterface $logger) {}
    public function handle(Event $event): void {
        if (!$event instanceof BeforeUserDeletedEvent) { return; }
        $result = $this->bridge->request($event->getUser()->getUID(), 'DELETE', 'account');
        if ($result['status'] !== 200) {
            $this->logger->warning('iCloud Drive Bridge account cleanup failed during user deletion. Remove the orphaned bridge account after restoring connectivity.', ['app' => 'icloud_drive']);
        }
    }
}
