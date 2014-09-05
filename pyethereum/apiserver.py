import logging
import threading
import json

import bottle

from pyethereum.chainmanager import chain_manager
from pyethereum.peermanager import peer_manager
import pyethereum.dispatch as dispatch
from pyethereum.blocks import block_structure, Block
import pyethereum.signals as signals
from pyethereum.transactions import Transaction
import pyethereum.processblock as processblock
import pyethereum.utils as utils
import pyethereum.rlp as rlp
from ._version import get_versions

logger = logging.getLogger(__name__)

app = bottle.Bottle()
app.config['autojson'] = False
app.install(bottle.JSONPlugin(json_dumps=lambda s: json.dumps(s, sort_keys=True)))


class ApiServer(threading.Thread):

    def __init__(self):
        super(ApiServer, self).__init__()
        self.daemon = True
        self.listen_host = '127.0.0.1'
        self.port = 30203

    def configure(self, config):
        self.listen_host = config.get('api', 'listen_host')
        self.port = config.getint('api', 'listen_port')
        # add api_path to bottle to be used in the middleware
        api_path = config.get('api', 'api_path')
        assert api_path.startswith('/') and not api_path.endswith('/')
        app.api_path = api_path

    def run(self):
        middleware = Middleware(app)
        bottle.run(middleware, server='waitress', host=self.listen_host, port=self.port)

# ###### create server ######

api_server = ApiServer()

@dispatch.receiver(signals.config_ready)
def config_api_server(sender, config, **kwargs):
    api_server.configure(config)


# ####### CORS, LOFFING AND REWRITE MIDDLEWARE ##############
class Middleware:
    HEADERS = [
        ('Access-Control-Allow-Origin', '*'),
        ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
        ('Access-Control-Allow-Headers',
         'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token')
    ]

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # strip api prefix from path
        orig_path = environ['PATH_INFO']
        if orig_path.startswith(self.app.api_path):
            environ['PATH_INFO'] = orig_path.replace(self.app.api_path, '', 1)

        logger.debug('%r: %s => %s', environ['REQUEST_METHOD'], orig_path, environ['PATH_INFO'])

        if environ["REQUEST_METHOD"] == "OPTIONS":
            start_response('200 OK', self.HEADERS + [('Content-Length', "0")])
            return ""
        else:
            def my_start_response(status, headers, exc_info=None):
                headers.extend(self.HEADERS)
                return start_response(status, headers, exc_info)
            return self.app(environ, my_start_response)


# ######### Utilities ########
def load_json_req():
    json_body = bottle.request.json
    if not json_body:
        json_body = json.load(bottle.request.body)
    return json_body


# ######## Version ###########
@app.get('/version/')
def version():
    logger.debug('version')
    v = get_versions()
    v['name'] = 'pyethereum'
    return dict(version=v)


# ######## Blocks ############
def make_blocks_response(blocks):
    res = []
    for b in blocks:
        h = b.to_dict()
        h['hash'] = b.hex_hash()
        h['chain_difficulty'] = b.chain_difficulty()
        res.append(h)
    return dict(blocks=res)


@app.get('/blocks/')
def blocks():
    logger.debug('blocks/')
    return make_blocks_response(chain_manager.get_chain(start='', count=20))

@app.get('/blocks/<arg>')
def block(arg=None):
    """
    /blocks/            return N last blocks
    /blocks/head        return head
    /blocks/<int>       return block by number
    /blocks/<hex>       return block by hexhash
    """
    logger.debug('blocks/%s', arg)
    try:
        if arg is None:
            return blocks()
        elif arg == 'head':
            blk = chain_manager.head
        elif arg.isdigit():
            blk = chain_manager.get(chain_manager.index.get_block_by_number(int(arg)))
        else:
            try:
                h = arg.decode('hex')
            except TypeError:
                raise KeyError
            blk = chain_manager.get(h)
    except KeyError:
        return bottle.abort(404, 'Unknown Block  %s' % arg)
    return make_blocks_response([blk])


# ######## Transactions ############
def make_transaction_response(txs):
    return dict(transactions = [tx.to_dict() for tx in txs])

@app.put('/transactions/')
def add_transaction():
    # request.json FIXME / post json encoded data? i.e. the representation of
    # a tx
    hex_data = bottle.request.body.read()
    logger.debug('PUT transactions/ %s', hex_data)
    tx = Transaction.hex_deserialize(hex_data)
    signals.local_transaction_received.send(sender=None, transaction=tx)
    return bottle.redirect('/transactions/' + tx.hex_hash())


@app.get('/transactions/<arg>')
def get_transactions(arg=None):
    """
    /transactions/<hex>          return transaction by hexhash
    """
    logger.debug('GET transactions/%s', arg)
    try:
        tx_hash = arg.decode('hex')
    except TypeError:
        bottle.abort(500, 'No hex  %s' % arg)
    try: # index
        tx, blk = chain_manager.index.get_transaction(tx_hash)
    except KeyError:
        # try miner
        txs = chain_manager.miner.get_transactions()
        found = [tx for tx in txs if tx.hex_hash() == arg]
        if not found:
            return bottle.abort(404, 'Unknown Transaction  %s' % arg)
        tx, blk = found[0], chain_manager.miner.block
    # response
    tx = tx.to_dict()
    tx['block'] = blk.hex_hash()
    if not chain_manager.in_main_branch(blk):
        tx['confirmations'] = 0
    else:
        tx['confirmations'] = chain_manager.head.number - blk.number
    return dict(transactions=[tx])


@app.get('/pending/')
def get_pending():
    """
    /pending/       return pending transactions
    """
    return dict(transactions=[tx.to_dict() for tx in chain_manager.miner.get_transactions()])



# ########### Trace ############

class TraceLogHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.buffer = []

    def emit(self, record):
        self.buffer.append(record)


def _get_block_before_tx(txhash):
    tx, blk = chain_manager.index.get_transaction(txhash.decode('hex'))
    # get the state we had before this transaction
    test_blk = Block.init_from_parent(blk.get_parent(),
                                        blk.coinbase,
                                        extra_data=blk.extra_data,
                                        timestamp=blk.timestamp,
                                        uncles=blk.uncles)
    pre_state = test_blk.state_root
    for i in range(blk.transaction_count):
        tx_lst_serialized, sr, _ = blk.get_transaction(i)
        if utils.sha3(rlp.encode(tx_lst_serialized)) == tx.hash:
            break
        else:
            pre_state = sr
    test_blk.state.root_hash = pre_state
    return test_blk, tx

@app.get('/trace/<txhash>')
def trace(txhash):
    """
    /trace/<hexhash>        return trace for transaction
    """
    logger.debug('GET trace/%s', txhash)
    try: # index
        test_blk, tx = _get_block_before_tx(txhash)
    except (KeyError, TypeError):
        return bottle.abort(404, 'Unknown Transaction  %s' % txhash)

    # collect debug output
    log = []
    def log_receiver(name, data):
        log.append({name:data})

    processblock.pblogger.listeners.append(log_receiver)

    # apply tx (thread? we don't want logs from other invocations)
    processblock.apply_transaction(test_blk, tx)

    # stop collecting debug output
    processblock.pblogger.listeners.remove(log_receiver)

    # format
    return dict(tx=txhash, trace=log)


@app.get('/dump/<txblkhash>')
def dump(txblkhash):
    """
    /dump/<hash>        return state dump after transaction or block
    """
    logger.debug('GET dump/%s', txblkhash)
    try:
        blk = chain_manager.get(txblkhash.decode('hex'))
    except:
        try: # index
            test_blk, tx = _get_block_before_tx(txblkhash)
        except (KeyError, TypeError):
            return bottle.abort(404, 'Unknown Transaction  %s' % txblkhash)
        processblock.apply_transaction(test_blk, tx)
        blk = test_blk
    # format
    return blk.to_dict(with_state=True)



# ######## Accounts ############
@app.get('/accounts/')
def accounts():
    logger.debug('accounts')

@app.get('/accounts/<address>')
def account(address=None):
    logger.debug('accounts/%s', address)
    data = chain_manager.head.account_to_dict(address)
    logger.debug(data)
    return data



# ######## Peers ###################
def make_peers_response(peers):
    objs = [dict(ip=ip, port=port, node_id=node_id.encode('hex'))
            for (ip, port, node_id) in peers]
    return dict(peers=objs)


@app.get('/peers/connected')
def connected_peers():
    return make_peers_response(peer_manager.get_connected_peer_addresses())


@app.get('/peers/known')
def known_peers():
    return make_peers_response(peer_manager.get_known_peer_addresses())
