VENDOR_PAGE_FIRST = 0xFF00
VENDOR_PAGE_LAST = 0xFFFF
HIDPP_LONG_REPORT = 0x11
DJ_REPORTS = (0x20, 0x21)


def isHidppDescriptor(data):
    # walks the items as HID 1.11 lays them out, where a short item's low two prefix bits give its size
    pages = []
    usagePageCount = 0
    reportIds = []
    i = 0
    while (i < len(data)):
        prefix = data[i]
        if (prefix == 0xFE):
            if (i + 1 >= len(data)):
                return False
            end = i + 3 + data[i + 1]
            if (end > len(data)):
                return False
            i = end
            continue
        size = [0, 1, 2, 4][prefix & 0x03]
        if (i + 1 + size > len(data)):
            return False
        value = int.from_bytes(data[i + 1:i + 1 + size], "little")
        kind = (prefix >> 2) & 0x03
        tag = prefix >> 4
        if (kind == 1 and tag == 0):
            pages.append(value)
            usagePageCount += 1
        elif (kind == 1 and tag == 8):
            reportIds.append(value)
        elif (kind == 2 and tag in (0, 1, 2) and size == 4):
            pages.append(value >> 16)
        i += 1 + size
    if (usagePageCount == 0):
        return False
    if (any(page < VENDOR_PAGE_FIRST or page > VENDOR_PAGE_LAST for page in pages)):
        return False
    if (HIDPP_LONG_REPORT not in reportIds):
        return False
    return not any(reportId in DJ_REPORTS for reportId in reportIds)
