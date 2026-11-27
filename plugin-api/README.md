# fetchbox-plugin-api

The [Fetchbox](https://github.com/MichaelYagi/fetchbox) plugin API, version 1: the base
classes, types and test kit a plugin is written against.

```python
from fetchbox_plugin import Addon, Plugin, Permission, on

class Hello(Addon):
    id = "hello"
    name = "Hello"

    @on("file.imported")
    async def wave(self, ctx, event):
        await ctx.files.add_sidecar(event.file, "hello.txt", b"Hello from a plugin\n")

class HelloPlugin(Plugin):
    id = "hello"
    name = "Hello"
    permissions = {Permission.LIBRARY_FILES}
    contributes = [Hello]
```

Within API 1 everything here only grows: nothing is removed, renamed or changes meaning.
Fetchbox includes this package, so a plugin needs only `fetchbox-plugin-api` as a dependency.

The guide, with everything the API offers and the test kit:
[Writing Fetchbox plugins](https://github.com/MichaelYagi/fetchbox/blob/main/docs/plugins.md).
Every class and function: [the API reference](https://github.com/MichaelYagi/fetchbox/blob/main/docs/plugin-api.md).
