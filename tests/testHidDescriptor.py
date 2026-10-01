import unittest

import hidDescriptor
from tests.fakes.descriptors import BOLT_KEYBOARD_DESCRIPTOR, C54F_DESCRIPTOR, FF00_HIDPP_DESCRIPTOR, GENERIC_DESKTOP_DESCRIPTOR


class DescriptorTests(unittest.TestCase):
    def testReceiverHidppDescriptorPasses(self):
        self.assertTrue(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR))

    def testGenericDesktopFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(GENERIC_DESKTOP_DESCRIPTOR))

    def testTheLightspeedAndBoltHidppInterfacePasses(self):
        self.assertTrue(hidDescriptor.isHidppDescriptor(FF00_HIDPP_DESCRIPTOR))

    def testTheBoltKeyboardInterfaceFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(BOLT_KEYBOARD_DESCRIPTOR))

    def testEitherDjReportAloneRejects(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("85 20")))
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("85 21")))

    def testKeyboardCollectionInAVendorDescriptorFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("05 07 09 06 a1 01 c0")))

    def testExtendedUsageFromGenericDesktopFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("0b 02 00 01 00")))

    def testExtendedUsageMinimumFromTheKeyboardPageFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("1b e0 00 07 00")))

    def testExtendedUsageMaximumFromTheKeyboardPageFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("2b e7 00 07 00")))

    def testNoUsagePageFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(bytes.fromhex("85 11 09 01 c0")))

    def testExtendedUsageAloneIsNotAUsagePage(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(bytes.fromhex("0b 01 00 00 ff 85 11")))

    def testMissingLongReportFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(bytes.fromhex("06 43 ff 0a 01 03 a1 01 85 10 c0")))

    def testTruncatedItemFails(self):
        # a Report ID item missing its data byte, which only the bounds check can reject
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("85")))

    def testALongItemFails(self):
        # the kernel refuses a descriptor with any long item, so the check does too
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("fe 02 10 05 07")))

    def testAnyTagFifteenPrefixFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("f3 00 01 05 01")))

    def testALoneTrailingLongPrefixFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("fe")))

    def testAFourByteUsagePageAboveTheVendorRangeFails(self):
        # page 0xFF000007 passes the lower bound, so only the upper bound at VENDOR_PAGE_LAST catches it
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("07 07 00 00 ff")))

    def testAFourByteVendorUsagePagePasses(self):
        self.assertTrue(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("07 00 ff 00 00")))

    def testTruncatedLongItemFails(self):
        self.assertFalse(hidDescriptor.isHidppDescriptor(C54F_DESCRIPTOR + bytes.fromhex("fe 05 10 05")))

    def testHidppReexportsTheCheck(self):
        import hidpp
        self.assertIs(hidpp.isHidppDescriptor, hidDescriptor.isHidppDescriptor)


if (__name__ == "__main__"):
    unittest.main()
