import unittest

from shop_env.slot_lease_pool import SlotLeasePool


class SlotLeasePoolTest(unittest.TestCase):
    def test_terminal_owner_keeps_slot_until_explicit_release(self):
        pool = SlotLeasePool(1)
        slot = pool.acquire()
        self.assertEqual(slot, 0)
        self.assertIsNone(pool.acquire())
        self.assertTrue(pool.release(slot))
        self.assertEqual(pool.acquire(), 0)

    def test_lease_token_rejects_stale_owner(self):
        pool = SlotLeasePool(1)
        slot = pool.acquire()
        lease = pool.lease_for(slot)
        self.assertTrue(pool.verify(slot, lease))
        self.assertFalse(pool.release(slot, "wrong"))
        self.assertTrue(pool.verify(slot, lease))
        self.assertTrue(pool.release(slot, lease))
        slot2 = pool.acquire()
        lease2 = pool.lease_for(slot2)
        self.assertNotEqual(lease, lease2)
        self.assertFalse(pool.verify(slot2, lease))

        pool = SlotLeasePool(1)
        slot = pool.acquire()
        self.assertTrue(pool.release(slot))
        self.assertFalse(pool.release(slot))

    def test_reset_recovers_all_slots(self):
        pool = SlotLeasePool(3)
        pool.acquire()
        pool.acquire()
        pool.reset(3)
        self.assertEqual(pool.free_slots(), frozenset({0, 1, 2}))


if __name__ == "__main__":
    unittest.main()
