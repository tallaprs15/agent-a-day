## Summary
This migration plan details the transition of Python applications from the legacy Azure Storage Blob SDK v2.1 `BlockBlobService` client to the current v12 `BlobServiceClient` and its associated hierarchical client objects (`ContainerClient` and `BlobClient`). Version 12 replaces monolithic service usage with dedicated clients and modern authentication options such as `DefaultAzureCredential`.

## Breaking changes

| Old API | New API | Evidence |
| :--- | :--- | :--- |
| `BlockBlobService` | `BlobServiceClient` | `https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme?view=azure-python#getting-started` |

## Migration steps
1. Uninstall legacy packages and install the current packages: `pip install azure-storage-blob azure-identity`.
2. Replace `BlockBlobService` imports with `BlobServiceClient` and other necessary client classes from `azure.storage.blob`.
3. Update client initialization from using `BlockBlobService(account_name=..., account_key=...)` to `BlobServiceClient(account_url=..., credential=...)` or `BlobServiceClient.from_connection_string(...)`.
4. Refactor container and blob operations to use hierarchical client retrieval methods like `blob_service_client.get_container_client()` and `blob_service_client.get_blob_client()`.

## Before and after

**Before (Legacy v2.1):**
```python
block_blob_service = BlockBlobService(account_name='myaccount',
account_key='mykey',
endpoint_suffix='local.azurestack.external')
```

**After (Current v12):**
```python
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

account_url = "https://<storage-account-name>.blob.core.windows.net"
credential = DefaultAzureCredential()
blob_service_client = BlobServiceClient(account_url, credential=credential)
```

## Not covered
Advanced connection parameters and custom retry policy configurations specific to custom endpoints not directly highlighted in the selected documentation snippets.

## Sources
- https://learn.microsoft.com/azure-stack/user/azure-stack-storage-dev?view=azs-2604#examples-2
- https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme?view=azure-python#getting-started
- https://learn.microsoft.com/azure/storage/blobs/storage-blob-container-create-python#set-up-your-environment
- https://learn.microsoft.com/azure/storage/blobs/storage-blob-upload-python#set-up-your-environment
- https://learn.microsoft.com/azure/storage/blobs/storage-blob-python-get-started
- https://learn.microsoft.com/azure/storage/blobs/storage-blob-copy-url-python#copy-a-blob-from-a-source-object-url
- https://learn.microsoft.com/azure/developer/python/sdk/fundamentals/common-types-response#deserialized-python-objects