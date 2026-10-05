<?php
declare(strict_types=1);
return [
    'routes' => [
        ['name' => 'page#index', 'url' => '/', 'verb' => 'GET'],
    ],
    'ocs' => [
        ['name' => 'admin#get', 'url' => '/api/admin', 'verb' => 'GET'],
        ['name' => 'admin#save', 'url' => '/api/admin', 'verb' => 'POST'],
        ['name' => 'api#get', 'url' => '/api/{endpoint}', 'verb' => 'GET', 'requirements' => ['endpoint' => '.+']],
        ['name' => 'api#post', 'url' => '/api/{endpoint}', 'verb' => 'POST', 'requirements' => ['endpoint' => '.+']],
        ['name' => 'api#put', 'url' => '/api/{endpoint}', 'verb' => 'PUT', 'requirements' => ['endpoint' => '.+']],
        ['name' => 'api#delete', 'url' => '/api/{endpoint}', 'verb' => 'DELETE', 'requirements' => ['endpoint' => '.+']],
    ],
];
