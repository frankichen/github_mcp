import os


_TEST_ENV = {
    "GITHUB_TOKEN": "test_token_value",
    "ACTION_API_KEY": "test_api_key_32_bytes_long",
    "ALLOWED_REPOSITORIES": "owner/allowed-repo",
    "ALLOW_DEFAULT_BRANCH_WRITE": "false",
    "MAX_FILE_CHARACTERS": "5000",
    "MAX_TOTAL_CHARACTERS": "10000",
    "MAX_FILES_PER_COMMIT": "5",
    "IDEMPOTENCY_DB_PATH": "/tmp/github-action-service-tests-idempotency.db",
    "CI_DB_PATH": "/tmp/github-action-service-tests-ci.db",
    "DEPLOYMENT_DB_PATH": "/tmp/github-action-service-tests-deployments.db",
    "INFRASTRUCTURE_DEPLOYMENT_DB_PATH": "/tmp/github-action-service-tests-infrastructure-deployments.db",
    "MYGITHUB12_DB_PATH": "/tmp/github-action-service-tests-mygithub12.db",
}

for _name, _value in _TEST_ENV.items():
    os.environ.setdefault(_name, _value)
