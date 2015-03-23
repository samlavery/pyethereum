import time
import struct
from pyethereum import blocks
from pyethereum import processblock
from pyethereum import utils
import rlp
from rlp.utils import encode_hex
from pyethereum.slogging import get_logger
log = get_logger('eth.miner')
pyethash = None


class Miner():
    """
    Mines on the current head
    Stores received transactions

    The process of finalising a block involves four stages:
    1) Validate (or, if mining, determine) uncles;
    2) validate (or, if mining, determine) transactions;
    3) apply rewards;
    4) verify (or, if mining, compute a valid) state and nonce.
    """

    def __init__(self, parent, uncles, coinbase):
        self.nonce = 0
        self.db = parent.db
        ts = max(int(time.time()), parent.timestamp + 1)
        self.block = blocks.Block.init_from_parent(parent, coinbase, extra_data='', timestamp=ts,
                                                   uncles=[u.header for u in uncles][:2])
        self.pre_finalize_state_root = self.block.state_root
        self.block.finalize()
        log.debug('mining', block_number=self.block.number,
                  block_hash=encode_hex(self.block.hash),
                  block_difficulty=self.block.difficulty)
        global pyethash
        if not pyethash:
            pyethash = __import__('pyethash')

    def add_transaction(self, transaction):
        old_state_root = self.block.state_root
        # revert finalization
        self.block.state_root = self.pre_finalize_state_root
        try:
            success, output = processblock.apply_transaction(self.block, transaction)
        except processblock.InvalidTransaction as e:
            # if unsuccessfull the prerequistes were not fullfilled
            # and the tx isinvalid, state must not have changed
            log.debug('invalid tx', tx_hash=transaction, error=e)
            success = False

        # finalize
        self.pre_finalize_state_root = self.block.state_root
        self.block.finalize()

        if not success:
            log.debug('tx not applied', tx_hash=transaction)
            assert old_state_root == self.block.state_root
            return False
        else:
            assert transaction in self.block.get_transactions()
            log.debug('transaction applied', tx_hash=transaction,
                      block_hash=self.block, result=output)
            assert old_state_root != self.block.state_root
            return True

    def get_transactions(self):
        return self.block.get_transactions()

    def mine(self, steps=1000):
        """
        It is formally defined as PoW: PoW(H, n) = BE(SHA3(SHA3(RLP(Hn)) o n))
        where:
        RLP(Hn) is the RLP encoding of the block header H, not including the
            final nonce component;
        SHA3 is the SHA3 hash function accepting an arbitrary length series of
            bytes and evaluating to a series of 32 bytes (i.e. 256-bit);
        n is the nonce, a series of 32 bytes;
        o is the series concatenation operator;
        BE(X) evaluates to the value equal to X when interpreted as a
            big-endian-encoded integer.
        """
        target = 2 ** 256 / self.block.difficulty
        rlp_Hn = rlp.encode(self.block.header,
                            blocks.BlockHeader.exclude(['nonce']))

        for nonce in range(self.nonce, self.nonce + steps):
            nonce_bin = struct.pack('>q', nonce)
            # BE(SHA3(SHA3(RLP(Hn)) o n))
            h = utils.sha3(utils.sha3(rlp_Hn) + nonce_bin)
            l256 = utils.big_endian_to_int(h)
            if l256 < target:
                self.block.nonce = nonce_bin
                assert self.block.header.check_pow() is True
                assert self.block.get_parent()
                log.debug('nonce found', block_nonce=nonce,
                          block_hash=encode_hex(self.block.hash))
                return self.block

        self.nonce = nonce
        return False
