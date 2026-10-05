<?php
declare(strict_types=1);
\OCP\Util::addStyle('icloud_drive', 'app');
\OCP\Util::addScript('icloud_drive', 'app');
?>
<div id="icloud-bridge" data-admin="<?php p($_['isAdmin'] ? 'true' : 'false'); ?>">
    <div class="ib-loading" role="status">iCloud Drive Bridge…</div>
</div>
