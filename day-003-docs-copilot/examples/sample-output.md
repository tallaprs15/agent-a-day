## Answer
To upload a local file to Azure Blob Storage using the Python SDK, you can create a `BlobClient` using either a connection string or an Azure credential and storage URL. Then, open the local file in binary read mode (`"rb"`) and call the `upload_blob()` method on the client object. 

Here is an example using a connection string:

```python
from azure.storage.blob import BlobClient

blob = BlobClient.from_connection_string(conn_str="<connection_string>", container_name="mycontainer", blob_name="my_blob")

with open("./SampleSource.txt", "rb") as data:
    blob.upload_blob(data)
```

## Version notes
none stated

## Sources
- https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme?view=azure-python#examples
- https://learn.microsoft.com/azure/developer/python/sdk/examples/azure-sdk-example-storage-use#4-use-blob-storage-from-app-code