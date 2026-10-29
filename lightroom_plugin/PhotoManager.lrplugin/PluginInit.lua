local LrApplication = import "LrApplication"
local LrExportSession = import "LrExportSession"
local LrFileUtils = import "LrFileUtils"
local LrJson = import "LrJson"
local LrPathUtils = import "LrPathUtils"
local LrTasks = import "LrTasks"

LiteGalleryBridgeStopping = false

local function child(parent, name)
    return LrPathUtils.child(parent, name)
end

local function bridgeRoot()
    local localAppData = os.getenv("LOCALAPPDATA")
    if not localAppData then
        return nil
    end
    return child(child(localAppData, "LiteGallery"), "lightroom-bridge")
end

local function readFile(path)
    local file = io.open(path, "rb")
    if not file then
        return nil
    end
    local contents = file:read("*a")
    file:close()
    return contents
end

local function writeAtomic(path, contents)
    local temporary = path .. ".tmp"
    local file, openError = io.open(temporary, "wb")
    if not file then
        error(openError)
    end
    file:write(contents)
    file:close()

    local renamed, renameError = os.rename(temporary, path)
    if not renamed then
        os.remove(temporary)
        error(renameError)
    end
end

local function applyRequest(request, catalog)
    if request.action == "import" then
        if not LrFileUtils.exists(request.path) then
            error("Source file no longer exists.")
        end
        local photo = catalog:findPhotoByPath(request.path)
        if not photo then
            catalog:addPhoto(request.path)
        end
    elseif request.action == "remove" then
        local photo = catalog:findPhotoByPath(request.path)
        if photo then
            catalog:removePhotos({ photo })
        end
    else
        error("Unsupported action.")
    end
end

local function exportRenderedPhoto(request)
    if not request.output_path then
        error("Rendered output directory was not provided.")
    end
    local catalog = LrApplication.activeCatalog()
    local photo = catalog:findPhotoByPath(request.path)
    if not photo then
        error("Photo is not present in the active Lightroom Catalog.")
    end

    LrFileUtils.createAllDirectories(request.output_path)
    local exportSession = LrExportSession({
        photosToExport = { photo },
        exportSettings = {
            LR_export_destinationType = "specificFolder",
            LR_export_destinationPathPrefix = request.output_path,
            LR_export_useSubfolder = false,
            LR_renamingTokensOn = false,
            LR_reimportExportedPhoto = false,
            LR_format = "TIFF",
            LR_tiff_bitDepth = 16,
            LR_tiff_compression = "compressionMethod_None",
            LR_export_colorSpace = "sRGB",
            LR_size_doConstrain = false,
        },
    })

    for _, rendition in exportSession:renditions({ stopIfCanceled = true }) do
        local success, result = rendition:waitForRender()
        if not success then
            error(tostring(result or "Lightroom render failed."))
        end
    end

    local renderedPath
    local files = LrFileUtils.files(request.output_path)
    if files then
        for filePath in files do
            local lowerPath = filePath:lower()
            if lowerPath:match("%.tif$") or lowerPath:match("%.tiff$") then
                renderedPath = filePath
                break
            end
        end
    end
    if not renderedPath then
        error("Lightroom completed the export but no TIFF was found.")
    end
    return renderedPath
end

local function processRequest(requestPath, ackFolder)
    local contents = readFile(requestPath)
    if not contents then
        return
    end

    local request = LrJson.decode(contents)
    if type(request) ~= "table"
        or type(request.operation_id) ~= "string"
        or type(request.path) ~= "string"
    then
        error("Invalid LiteGallery request.")
    end

    local renderedPath
    if request.action == "render" then
        renderedPath = exportRenderedPhoto(request)
    else
        local catalog = LrApplication.activeCatalog()
        catalog:withWriteAccessDo("LiteGallery Catalog Sync", function()
            applyRequest(request, catalog)
        end)
    end

    local acknowledgement = {
        operation_id = request.operation_id,
        action = request.action,
        status = "success",
        rendered_path = renderedPath,
    }
    local ackPath = child(ackFolder, request.operation_id .. ".json")
    writeAtomic(ackPath, LrJson.encode(acknowledgement))
    os.remove(requestPath)
end

local function writeFailureAck(requestPath, ackFolder, errorMessage)
    local operationId = requestPath:match("([^/\\]+)%.json$")
    if not operationId then
        return
    end
    local acknowledgement = {
        operation_id = operationId,
        status = "error",
        error = tostring(errorMessage),
    }
    local ackPath = child(ackFolder, operationId .. ".json")
    local ok = pcall(function()
        writeAtomic(ackPath, LrJson.encode(acknowledgement))
    end)
    if ok then
        os.remove(requestPath)
    end
end

local function pollQueue()
    local root = bridgeRoot()
    if not root then
        return
    end
    local requestFolder = child(root, "requests")
    local ackFolder = child(root, "acks")
    LrFileUtils.createAllDirectories(requestFolder)
    LrFileUtils.createAllDirectories(ackFolder)

    while not LiteGalleryBridgeStopping do
        local ok, files = pcall(function()
            return LrFileUtils.files(requestFolder)
        end)
        if ok and files then
            for requestPath in files do
                if requestPath:match("%.json$") then
                    local processed, processError = pcall(function()
                        processRequest(requestPath, ackFolder)
                    end)
                    if not processed then
                        writeFailureAck(requestPath, ackFolder, processError)
                    end
                end
            end
        end
        LrTasks.sleep(2)
    end
end

LrTasks.startAsyncTask(pollQueue)
