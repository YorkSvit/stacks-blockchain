#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
"""

import json
import traceback
import binascii
import hashlib
import math
import keychain
import pybitcoin
import os
import sys
import copy
import shutil
import threading

from collections import defaultdict
from . import *
from ..config import *
from ..operations import *
from ..hashing import *
from ..b40 import is_b40

import virtualchain
from db import *

if not globals().has_key('log'):
    log = virtualchain.session.log


# singleton read/write instance
blockstack_db = None
blockstack_db_lastblock = None
blockstack_db_lock = threading.Lock()

DISPOSITION_RO = "readonly"
DISPOSITION_RW = "readwrite"


def autofill( *autofill_fields ):
    """
    Decorator to automatically fill in extra useful fields
    that aren't stored in the db.
    """
    def wrap( reader ):
        def wrapped_reader( *args, **kw ):
            rec = reader( *args, **kw )
            if rec is not None:
                for field in autofill_fields:
                    if field == "opcode" and 'opcode' not in rec.keys():
                        assert 'op' in rec.keys(), "BUG: record is missing 'op'"
                        rec['opcode'] = op_get_opcode_name(rec['op'])
                    else:
                        raise Exception("Unknown autofill field '%s'" % field)

            return rec
        return wrapped_reader
    return wrap


class BlockstackDB( virtualchain.StateEngine ):
    """
    State engine implementatoin for blockstack.
    """

    def __init__( self, db_filename, disposition, expected_snapshots={} ):
        """
        Construct the Blockstack State Engine
        from locally-cached db state.

        DO NOT CALL THIS DIRECTLY
        """

	if expected_snapshots == {}:
           expected_snapshots = {'snapshots':{}}

        log.debug("Initialize database from '%s'" % db_filename )
        self.db = None

        # make this usable if virtualchain hasn't been configured yet
        # (i.e. if we're importing this class directly)
        import virtualchain_hooks
        blockstack_impl = virtualchain.get_implementation()
        if blockstack_impl is None:
            blockstack_impl = virtualchain_hooks
      
        if not os.environ.has_key("BLOCKSTACK_TESTSET") or os.environ["BLOCKSTACK_TESTSET"] != "1":
            initial_snapshots = GENESIS_SNAPSHOT
            magic_bytes = MAGIC_BYTES_MAINSET
        else:
            initial_snapshots = GENESIS_SNAPSHOT_TESTSET
            magic_Bytes = MAGIC_BYTES_TESTSET

        # acquire the database
        self.db_filename = db_filename
        if os.path.exists( db_filename ):
            self.db = namedb_open( db_filename )
        else:
            self.db = namedb_create( db_filename )

        self.disposition = disposition

        # are we resuming part-way through committing transactions?
        lastblock = self.get_lastblock( impl=blockstack_impl )
        resume_offset = 0
        if lastblock is not None:

            # how many vtxs are in the upcoming block?
            num_vtxs = self.get_saved_num_vtxs( lastblock + 1, impl=blockstack_impl )
            if num_vtxs is not None:
                
                # resume where we left off
                cur = self.db.cursor()
                vtxs_processed = namedb_get_num_block_vtxs( cur, lastblock + 1 )
                resume_offset = vtxs_processed


        super( BlockstackDB, self ).__init__( magic_bytes,
                                              OPCODES,
                                              BlockstackDB.make_opfields(),
                                              impl=blockstack_impl,
                                              initial_snapshots=initial_snapshots,
                                              state=self,
                                              expected_snapshots=expected_snapshots,
                                              resume_offset=resume_offset )

        # announcers to track
        blockstack_opts = default_blockstack_opts( virtualchain.get_config_filename(impl=blockstack_impl) )
        self.announce_ids = blockstack_opts['announcers'].split(",")

        self.set_backup_frequency( blockstack_opts['backup_frequency'] )
        self.set_backup_max_age( blockstack_opts['backup_max_age'] )

        # collision detection 
        # map block_id --> history_id_key --> list of history ID values
        self.collisions = {}


    @classmethod 
    def borrow_readwrite_instance( cls, db_path, block_number, expected_snapshots={} ):
        """
        Singleton--ensure at most one read/write connection to the db exists.
        """

        global blockstack_db, blockstack_db_lastblock, blockstack_db_lock

        blockstack_db_lock.acquire()

        try:
            assert blockstack_db is None, "Borrowing violation"
        except Exception, e:
            log.exception(e)
            log.error("FATAL: Borrowing violation")
            sys.exit(1)

        blockstack_db = BlockstackDB( db_path, DISPOSITION_RW, expected_snapshots=expected_snapshots )
        blockstack_db_lastblock = block_number
        blockstack_db_lock.release()

        return blockstack_db


    @classmethod
    def release_readwrite_instance( cls, db_inst, block_number ):
        """
        Singleton--ensure at most one read/write connection to the db exists.
        """

        global blockstack_db, blockstack_db_lastblock, blockstack_db_lock 

        blockstack_db_lock.acquire()

        try:
            assert blockstack_db is not None, "Borrowing return violation: db is None"
            assert blockstack_db == db_inst, "Borrowing return violation: different instances"
            assert blockstack_db_lastblock == block_number, "Borrowing return violation: different blocks"
        except Exception, e:
            log.exception(e)
            log.error("FATAL: Borrowing-release violation")
            sys.exit(1)

        del blockstack_db
        del db_inst

        db_inst = None
        blockstack_db = None
        blockstack_db_lastblock = None 

        blockstack_db_lock.release()
        return True


    @classmethod 
    def make_opfields( cls ):
        """
        Calculate the virtulachain-required opfields dict.
        """
        # construct fields 
        opfields = {}
        for opname in SERIALIZE_FIELDS.keys():
            opcode = NAME_OPCODES[opname]
            opfields[opcode] = SERIALIZE_FIELDS[opname]

        return opfields


    def get_db_path( self ):
        """
        Get db file path
        """
        return self.db_filename

    
    def export_db( self, path ):
        """
        Copy the database to the given location.
        """
        shutil.copyfile( self.get_db_path(), path )


    @classmethod
    def build_import_keychain( cls, namespace_id, pubkey_hex ):
        """
        Generate all possible NAME_IMPORT addresses from the NAMESPACE_REVEAL public key
        """

        pubkey_addr = pybitcoin.BitcoinPublicKey( str(pubkey_hex) ).address()

        # do we have a cached one on disk?
        cached_keychain = os.path.join( virtualchain.get_working_dir(), "%s.keychain" % namespace_id )
        if os.path.exists( cached_keychain ):

            child_addrs = []
            try:
                lines = []
                with open(cached_keychain, "r") as f:
                    lines = f.readlines()

                child_attrs = [l.strip() for l in lines]

                log.debug("Loaded cached import keychain for '%s' (%s)" % (pubkey_hex, pubkey_addr))
                return child_attrs

            except Exception, e:
                log.exception(e)
                pass

        pubkey_hex = str(pubkey_hex)
        public_keychain = keychain.PublicKeychain.from_public_key( pubkey_hex )
        child_addrs = []

        for i in xrange(0, NAME_IMPORT_KEYRING_SIZE):
            public_child = public_keychain.child(i)
            public_child_address = public_child.address()

            child_addrs.append( public_child_address )

            if i % 20 == 0 and i != 0:
                log.debug("%s children..." % i)

        # include this address
        child_addrs.append( pubkey_addr )

        log.debug("Done building import keychain for '%s' (%s)" % (pubkey_hex, pubkey_addr))

        # cache
        try:
            with open(cached_keychain, "w+") as f:
                for addr in child_addrs:
                    f.write("%s\n" % addr)

                f.flush()

            log.debug("Cached keychain to '%s'" % cached_keychain)
        except Exception, e:
            log.exception(e)
            log.error("Unable to cache keychain for '%s' (%s)" % (pubkey_hex, pubkey_addr))

        return child_addrs


    @classmethod
    def load_import_keychain( cls, namespace_id ):
        """
        Get an import keychain from disk.
        Return None if it doesn't exist.
        """
      
        # do we have a cached one on disk?
        cached_keychain = os.path.join( virtualchain.get_working_dir(), "%s.keychain" % namespace_id)
        if os.path.exists( cached_keychain ):

            log.debug("Load import keychain '%s'" % cached_keychain)
            child_addrs = []
            try:
                lines = []
                with open(cached_keychain, "r") as f:
                    lines = f.readlines()
 
                child_attrs = [l.strip() for l in lines]
 
                log.debug("Loaded cached import keychain for '%s'" % namespace_id)
                return child_attrs
 
            except Exception, e:
                log.exception(e)
                log.error("FATAL: uncaught exception loading the import keychain")
                sys.exit(1)
 
        else:
            log.debug("No import keychain at '%s'" % cached_keychain)

        return None

    
    def commit_get_cursor( self, block_number ):
        """
        Get a database cursor for a commit on this block number.
        """

        return self.db.cursor()


    def commit_finished( self, block_id ):
        """
        Called when the block is finished.
        Commits all data.
        """

        self.db.commit()
        self.clear_collisions( block_id )

    
    def log_accept( self, block_id, vtxindex, op, op_data ):
        """
        Log an accepted operation
        """

        opcode = op_data.get('opcode', None)
        debug_op = self.sanitize_op( op_data )
        if 'history' in debug_op:
            del debug_op['history'] 

        log.debug("ACCEPT %s at (%s, %s) data: %s", opcode, block_id, vtxindex, 
                ", ".join( ["%s='%s'" % (k, debug_op[k]) for k in sorted(debug_op.keys())] ) )

        return

 
    def log_commit( self, block_id, vtxindex, op, opcode, op_data ):
        """
        Log a committed operation
        """

        debug_op = self.sanitize_op( op_data )
        if 'history' in debug_op:
            del debug_op['history'] 

        log.debug("COMMIT %s (%s) at (%s, %s) data: %s", opcode, op, block_id, vtxindex, 
                ", ".join( ["%s='%s'" % (k, debug_op[k]) for k in sorted(debug_op.keys())] ) )

        return


    def log_reject( self, block_id, vtxindex, op, op_data ):
        """
        Log a rejected operation
        """

        debug_op = self.sanitize_op( op_data )
        if 'history' in debug_op:
            del debug_op['history']

        log.debug("REJECT %s at (%s, %s) data: %s", op_get_opcode_name( op ), block_id, vtxindex,
                ", ".join( ["%s='%s'" % (k, debug_op[k]) for k in sorted(debug_op.keys())] ))

        return


    def sanitize_op( self, op_data ):
        """
        Remove unnecessary fields for an operation, i.e. prior to committing it.
        This includes any invariant tags we've added with our invariant decorators
        (such as @state_create or @state_transition).
        
        TODO: less ad-hoc way to do this
        """
        
        op_data = super( BlockstackDB, self ).sanitize_op( op_data )

        # remove invariant tags (i.e. added by our invariant state_* decorators)
        to_remove = get_state_invariant_tags()
        for tag in to_remove:
            if tag in op_data.keys():
                del op_data[tag]

        # NOTE: this is called the opcode family, because
        # different operation names can have the same operation code
        # (such as NAME_RENEWAL and NAME_REGISTRATION).  They must
        # have the same mutation fields.
        opcode_family = op_get_opcode_name( op_data['op'] )

        # for each column in the appropriate state table,
        # if the column is not identified in the operation's
        # MUTATE_FIELDS list, then set it to None here.
        mutate_fields = op_get_mutate_fields( opcode_family )
        for mf in mutate_fields:
            if not op_data.has_key( mf ):
                log.debug("Adding NULL mutate field '%s.%s'" % (opcode_family, mf ))
                op_data[mf] = None

        # TODO: less ad-hoc
        for extra_field in ['opcode']:
            if extra_field in op_data:
                del op_data[extra_field]

        return op_data


    @classmethod
    def check_collision_state( cls, collision_state, history_id_key, history_id, block_id, checked_ops, affected_opcodes ):
        """
        Given a history ID, see if it already exists
        at the given block ID (i.e. it's not expired),
        using the given collision state.
        Return True if so; False if not.
        If there is a collision, set the __collided__ field in each checked_ops that
        has a matching history_id value and has an opcode in affected_opcodes.
        """

        # seen before in this block?
        if collision_state.has_key( block_id ):
            if collision_state[block_id].has_key(history_id_key):
                if history_id in collision_state[block_id][history_id_key]:
                    rc = True

                else:
                    collision_state[block_id][history_id_key].append( history_id )
                    rc = False

            else:
                collision_state[block_id][history_id_key] = [history_id]
                rc = False

        else:
            collision_state[block_id] = { history_id_key: [history_id] }
            rc = False

        if not rc:
            # no collision 
            return rc

        # find and mark collided operations 
        for prev_op in checked_ops:

            prev_opcode = op_get_opcode_name( prev_op['op'] )
            if prev_opcode not in affected_opcodes:
                # not affected
                continue 

            if history_id_key not in prev_op:
                # won't match
                continue

            if prev_op[history_id_key] == history_id:
                # collision 
                cls.nameop_set_collided( prev_op, history_id_key, history_id )

        return True


    def find_collisions( self, checked_ops ):
        """
        Given a list of checked operations, find the ones that collide.
        Return a dict structured as history_id_key --> [history_id]
        """

        collisions = {}
        for op in checked_ops:
            if BlockstackDB.nameop_is_collided( op ):
                BlockstackDB.nameop_put_collision( collisions, op )

        return collisions


    def put_collisions( self, block_id, collisions ):
        """
        Put collision state for a particular block.
        Any operations checked at this block_id that collide 
        with the given collision state will be rejected.
        """
        self.collisions[ block_id ] = copy.deepcopy( collisions )

    
    def clear_collisions( self, block_id ):
        """
        Clear out all collision state for a given block number
        """
        if block_id in self.collisions:
            del self.collisions[block_id]
        

    def check_collision( self, history_id_key, history_id, block_id, checked_ops, affected_opcodes ):
        """
        Given a history ID, see if it already exists
        at the given block ID (i.e. it's not expired).
        Return True if so; False if not.
        If there is a collision, set the __collided__ field in each checked_ops that
        has a matching history_id value and has an opcode in affected_opcodes.
        """

        return BlockstackDB.check_collision_state( self.collisions, history_id_key, history_id, block_id, checked_ops, affected_opcodes )


    def check_preorder_collision( self, preorder_hash, block_id, checked_ops ):
        """
        Are there any colliding preorders in this block?
        Set the '__collided__' flag if so, so we don't commit them.
        """

        return self.check_collision( "preorder_hash", preorder_hash, block_id, checked_ops, OPCODE_PREORDER_OPS )


    def check_name_collision( self, name, block_id, checked_ops ):
        """
        Are there any colliding names in this block?
        Set the '__collided__' flag if so, so we don't commit them.
        """

        return self.check_collision( "name", name, block_id, checked_ops, OPCODE_NAME_STATE_CREATIONS )

    
    def check_namespace_collision( self, namespace_id, block_id, checked_ops ):
        """
        Are there any colliding namespaces in this block?
        """

        return self.check_collision( "namespace_id", namespace_id, block_id, checked_ops, OPCODE_NAMESPACE_STATE_CREATIONS )


    def check_noop_collision( self, name, block_id, checked_ops ):
        """
        No-op collision detector.
        Meant for name-import
        """
        log.warn("No-op collision detection for '%s'" % name)
        return False


    @autofill( "opcode" )
    def get_namespace( self, namespace_id ):
        """
        Given a namespace ID, get the ready namespace op for it.

        Return the dict with the parameters on success.
        Return None if the namespace has not yet been revealed.
        """

        cur = self.db.cursor()
        return namedb_get_namespace_ready( cur, namespace_id )

    
    @autofill( "opcode" )
    def get_namespace_by_preorder( self, preorder_hash ):
        """
        Given a namespace preorder hash, get the associated namespace
        reveal or ready (it may be expired).
        """
        cur = self.db.cursor()
        return namedb_get_namespace_by_preorder_hash( cur, preorder_hash )


    @autofill( "opcode" )
    def get_name_by_preorder( self, preorder_hash ):
        """
        Given a name preorder hash, get the associated name record.
        (It may be expired or revoked)
        """
        cur = self.db.cursor()
        return namedb_get_name_by_preorder_hash( cur, preorder_hash )


    @autofill( "opcode" )
    def get_name( self, name, lastblock=None, include_expired=False ):
        """
        Given a name, return the latest version and history of
        the metadata gleaned from the blockchain.
        Name must be fully-qualified (i.e. name.ns_id)
        Return None if no such name is currently registered.

        NOTE: returns names that are revoked
        """

        if lastblock is None:
            lastblock = self.lastblock

        cur = self.db.cursor()
        name_rec = namedb_get_name( cur, name, lastblock, include_expired=include_expired )
        return name_rec


    def get_name_at( self, name, block_number ):
        """
        Generate and return the sequence of of states a name record was in
        at a particular block number.
        """

        name_rec = self.get_name( name )

        # trivial reject
        if name_rec is None:
            # never existed
            return None

        if block_number < name_rec['block_number']:
            # didn't exist then
            return None

        historical_recs = namedb_restore_from_history( name_rec, block_number )
        return historical_recs


    def get_namespace_at( self, namespace_id, block_number ):
        """
        Generate and return the sequence of states a namespace record was in
        at a particular block number.
        """

        namespace_rec = self.get_namespace( namespace_id )
        if namespace_rec is None:
            return None

        if block_number < namespace_rec['block_number']:
            return None

        historical_recs = namedb_restore_from_history( namespace_rec, block_number )
        return historical_recs

    
    def get_name_history( self, name, start_block, end_block ):
        """
        Get the sequence of states of a name over a given point in time.
        """

        name_rec = self.get_name( name )
        if name_rec is None:
            return None 

        cur = self.db.cursor()
        name_snapshots = []
        update_points = namedb_get_history_range( cur, name, start_block, end_block ) 
        for update_point in update_points:
             historical_recs = self.get_name_at( name, update_point )
             name_snapshots += historical_recs

        return name_snapshots


    def get_all_records_at( self, block_number, include_history=False ):
        """
        Get all records affected at a particular block,
        in the state they were at the given block number.
        """
        recs = namedb_get_all_records_at( self.db, block_number, include_history=include_history )
        return recs
        

    def get_name_from_name_hash128( self, name ):
        """
        Get the name from a name hash
        """
        cur = self.db.cursor()
        name = namedb_get_name_from_name_hash128( cur, name, self.lastblock )
        return name


    def get_names_owned_by_address( self, address ):
        """
        Get the set of names owned by a particular address.
        NOTE: only works for cases where we could extract an address.
        """

        cur = self.db.cursor()
        names = namedb_get_names_owned_by_address( cur, address, self.lastblock )
        return names

    
    def get_names_owned_by_sender( self, sender_pubkey, lastblock=None ):
        """
        Get the set of names owned by a particular script-pubkey.
        """

        cur = self.db.cursor()
        if lastblock is None:
            lastblock = self.lastblock 

        names = namedb_get_names_by_sender( cur, sender_pubkey, lastblock )
        return names


    def get_all_names( self, offset=None, count=None ):
        """
        Get the set of all registered names, with optional pagination
        Returns the list of names.
        """

        cur = self.db.cursor()
        names = namedb_get_all_names( cur, self.lastblock, offset=offset, count=count )
        return names

    
    def get_names_in_namespace( self, namespace_id, offset=None, count=None ):
        """
        Get the set of all registered names in a particular namespace.
        Returns the list of names.
        """

        cur = self.db.cursor()
        names = namedb_get_names_in_namespace( cur, namespace_id, self.lastblock, offset=offset, count=count )
        return names


    def get_all_namespace_ids( self ):
        """
        Get the set of all existing, READY namespace IDs.
        """

        cur = self.db.cursor()
        namespace_ids = namedb_get_all_namespace_ids( cur )
        return namesapce_ids
       

    def get_all_preordered_namespace_hashes( self ):
        """
        Get all oustanding namespace preorder hashes that have not expired.
        """

        cur = self.db.cursor()
        namespace_hashes = namedb_get_all_preordered_namespace_hashes( cur, self.lastblock )
        return namespace_hashes 


    def get_all_revealed_namespace_ids( self ):
        """
        Get all revealed namespace IDs that have not expired.
        """

        cur = self.db.cursor()
        namespace_ids = namedb_get_all_revealed_namespace_ids( cur, self.lastblock )
        return namespace_ids


    def get_all_importing_namespace_hashes( self ):
        """
        Get the set of all preordered and revealed namespace hashes that have not expired.
        """

        cur = self.db.cursor()
        namespace_hashes = namedb_get_all_importing_namespace_hashes( cur, self.lastblock )
        return namespace_hashes
        

    def get_name_from_name_consensus_hash( self, name_consensus_hash, sender_script_pubkey, block_id ):
        """
        Find the name.ns_id from hash( name.ns_id, consensus_hash ), given the sender and
        block_id, and assuming that name.ns_id is already registered.

        There are only a small number of values this hash can take, so test all of them to
        see if the hash matches one of them.

        This is used for name updates--we need to ensure that updates have timely consensus
        hashes, and are on the majority blockchian fork.

        Return (fully-qualified name, consensus hash) on success
        Return (None, None) if not found.
        """

        cur = self.db.cursor()
        names = namedb_get_names_by_sender( cur, sender_script_pubkey, self.lastblock )
        
        if names is None:
            log.error("Sender script '%s' owns no names" % sender_script_pubkey )
            return (None, None)

        possible_consensus_hashes = []
        for i in xrange( block_id - virtualchain.config.BLOCKS_CONSENSUS_HASH_IS_VALID, block_id+1 ):
            consensus_hash = self.get_consensus_at( i )
            if consensus_hash is not None and consensus_hash not in possible_consensus_hashes:
                possible_consensus_hashes.append( str(consensus_hash) )
    
        
        for name in names:
            for consensus_hash in possible_consensus_hashes:

                # what would have been the name/consensus_hash?
                test_name_consensus_hash = hash256_trunc128( str(name) + consensus_hash )
                if test_name_consensus_hash == name_consensus_hash:
                    # found!
                    return name, consensus_hash

        return None, None


    @autofill( "opcode" )
    def get_name_preorder( self, name, sender_script_pubkey, register_addr ):
        """
        Get the current preorder for a name, given the name, the sender's script pubkey, and
        the registration address used to calculate the preorder hash.

        Return the preorder record on success.
        Return None if not found, or the preorder is already registered.

        NOTE: possibly returns an expired preorder (by design, so as to prevent someone
        from re-sending the same preorder with the same preorder hash).
        """

        # name registered and not expired?
        name_rec = self.get_name( name )
        if name_rec is not None:
            return None

        preorder_hash = hash_name(name, sender_script_pubkey, register_addr=register_addr)
        preorder = namedb_get_name_preorder( self.db, preorder_hash, self.lastblock )
        return preorder 

    
    @autofill( "opcode" )
    def get_namespace_preorder( self, namespace_id_hash ):
        """
        Given the hash(namesapce_id, sender_script_pubkey, reveal_addr) for a 
        namespace that is being imported, get its associated NAMESPACE_PREORDER
        record.

        Return the namespace preorder record on success.
        Return None if not found, if the namespace is currently not preordered, or if the preorder record is expired.
        """

        namespace_preorder = namedb_get_namespace_preorder( self.db, namespace_id_hash, self.lastblock ) 
        return namespace_preorder


    def get_name_owner( self, name ):
        """
        Given a name, get its associated sender pubkey script.

        Return the string on success
        Return None if the name doesn't exist.
        """

        name_rec = self.get_name( name )
        if name_rec is None:
            return None

        else:
            return name_rec['sender']

    
    @autofill( "opcode" )
    def get_namespace_reveal( self, namespace_id ):
        """
        Given the name of a namespace, get it if it is currently
        being revealed.

        Return the reveal record on success.
        Return None if it is not being revealed, or is expired.
        """

        cur = self.db.cursor()
        namespace_reveal = namedb_get_namespace_reveal( cur, namespace_id, self.lastblock )
        return namespace_reveal


    def get_announce_ids( self ):
        """
        Get the set of announce IDs
        """
        return self.announce_ids


    def get_name_init_opcode( self, name, block_number=None, include_expired=False ):
        """
        Get the first opcode for this name.  I.e. an import, a preorder, etc.
        If block_number is not None, then first restore the name to the way
        it was at the block number.
        """

        if block_number is None:
            block_number = self.lastblock 

        name_rec = self.get_name( name, lastblock=block_number, include_expired=include_expired )
        if name_rec is None:
            return None

        name_hist = name_rec['history']
        blocks = sorted([int(b) for b in name_hist.keys()])
        opcode = None

        try:
            assert len(blocks) != 0, "FATAL: no history for name %s" % name
           
            first_hist = name_hist[blocks[0]][0]
            assert first_hist.has_key('op'), "FATAL: no op recorded for name %s" % name

            opcode = op_get_opcode_name( first_hist['op'] )
        except Exception, e:
            log.exception(e)
            log.error("FATAL: no history info")
            sys.exit(1)

        return opcode 
        

    def is_name_expired( self, name, block_number ):
        """
        Given a name and block number, determine if it is expired at that block.
        * names in revealed but not ready namespaces are never expired, unless the namespace itself is expired;
        * names in ready namespaces expire once max(ready_block, renew_block) + lifetime blocks passes

        Return True if so
        Return False if not, or if the name doesn't exist
        """
        cur = self.db.cursor()
        return namedb_get_name( cur, name, block_number ) is None


    def is_name_registered( self, name ):
        """
        Given the fully-qualified name, is it registered, not revoked, and not expired
        at the current block?
        """
        name_rec = self.get_name( name )    # won't return the name if expired
        if name_rec is None:
            return False 

        if name_rec['revoked']:
            return False

        return True


    def is_namespace_ready( self, namespace_id ):
        """
        Given a namespace ID, determine if the namespace is ready
        at the current block.
        """

        namespace = self.get_namespace( namespace_id )
        if namespace is not None:
            return True
        else:
            return False


    def is_namespace_preordered( self, namespace_id_hash ):
        """
        Given a namespace preorder hash, determine if it is preordered
        at the current block.
        """

        namespace_preorder = self.get_namespace_preorder( self.db, namespace_id_hash, self.lastblock )
        if namespace_preorder is None:
            return False 
        else:
            return True


    def is_namespace_revealed( self, namespace_id ):
        """
        Given the name of a namespace, has it been revealed but not made ready
        at the current block?
        """

        namespace_reveal = self.get_namespace_reveal( namespace_id )
        if namespace_reveal is not None:
            return True
        else:
            return False


    def is_name_owner( self, name, sender_script_pubkey ):
        """
        Given the fully-qualified name and a sender's script pubkey,
        determine if the sender owns the name.

        The name must exist and not be revoked or expired at the
        current block.
        """

        if not self.is_name_registered( name ):
            # no one owns it 
            return False 

        owner = self.get_name_owner( name )
        if owner != sender_script_pubkey:
            return False 
        else:
            return True


    def is_new_preorder( self, preorder_hash ):
        """
        Given a preorder hash of a name, determine whether or not it is unseen before.
        """

        preorder = namedb_get_name_preorder( self.db, preorder_hash, self.lastblock )
        if preorder is not None:
            return False
        else:
            return True


    def is_new_namespace_preorder( self, namespace_id_hash ):
        """
        Given a namespace preorder hash, determine whether or not is is unseen before.
        """

        preorder = namedb_get_namespace_preorder( self.db, namespace_id_hash, self.lastblock )
        if preorder is not None:
            return False 
        else:
            return True


    def is_name_revoked( self, name ):
        """
        Determine if a name is revoked at this block.
        """

        name = self.get_name( name )
        if name is None:
            return False 

        if name['revoked']:
            return True
        else:
            return False


    @classmethod
    def nameop_set_collided( cls, nameop, history_id_key, history_id ):
        """
        Mark a nameop as collided
        """
        nameop['__collided__'] = True
        nameop['__collided_history_id_key__'] = history_id_key 
        nameop['__collided_history_id__'] = history_id


    @classmethod
    def nameop_is_collided( cls, nameop ):
        """
        Is this nameop collided?
        """
        return '__collided__' in nameop and nameop['__collided__']


    @classmethod 
    def nameop_put_collision( cls, collisions, nameop ):
        """
        Record a nameop as collided in some collision state.
        """

        history_id_key = nameop.get('__collided_history_id_key__', None)
        history_id = nameop.get('__collided_history_id__', None)

        try:
            assert cls.nameop_is_collided( nameop ), "Nameop not collided"
            assert history_id_key is not None, "Nameop missing collision info"
            assert history_id is not None, "Nameop missing collision info"
        except Exception, e:
            log.exception(e)
            log.error("FATAL: BUG: bad collision info")
            sys.exit(1)
        
        if not collisions.has_key(history_id_key):
            collisions[history_id_key] = [history_id]
        else:
            collisions[history_id_key].append( history_id )


    def add_all_consensus_values( self, opcode, nameop, commit, current_block_number ):
        """
        Add all extra consensus-affecting fields that 
        are derived from the given name operation's fields.

        If @commit is True, then find only the values that will be written to the db.
        If @commit is False, then find all values that go into checking the operation.
        """
        
        consensus_extra = None 
        
        if commit:
            consensus_extra = op_commit_consensus_extra( opcode, nameop, current_block_number, self )
        else:
            consensus_extra = op_snv_consensus_extra( opcode, nameop, current_block_number, self )

        # must be non-conflicting, unless explicitly set otherwise
        overwrites = []
        for k in consensus_extra.keys():
            if k in nameop.keys():
                if nameop[k] != consensus_extra[k] and not op_commit_consensus_has_override( consensus_extra, k ):
                    overwrites.append(k)

        try:
            assert len(overwrites) == 0, "Derived consensus fields overwrites transaction data: %s" % ",".join(["%s -> %s" % (nameop[o], consensus_extra[o]) for o in overwrites])
        except Exception, e:
            log.exception(e)
            log.error("FATAL: BUG: tried to overwrite consensus data %s".join(overwrites))
            sys.exit(1)
       
        consensus_extra = op_commit_consensus_sanitize( consensus_extra )
        nameop.update( consensus_extra )
        return


    def add_all_commit_consensus_values( self, opcode, nameop, current_block_number ):
        """
        Find all consensus-affecting values in the operation that will also be committed
        to the database.  Add them to the nameop.
        """
        return self.add_all_consensus_values( opcode, nameop, True, current_block_number )

    
    def add_all_snv_consensus_values( self, opcode, nameop, current_block_number ):
        """
        Find all consensus-affecting values in the operation that will be used to check its
        validity.  Add them to the nameop.
        """
        return self.add_all_consensus_values( opcode, nameop, False, current_block_number )


    def commit_operation( self, nameop, current_block_number ):
        """
        Commit an operation, thereby carrying out a state transition.
        """
    
        cur = self.db.cursor()
        op_seq = None
        opcode = nameop.get('opcode', None)
        history_id = None

        try:
            assert opcode is not None, "Undefined op '%s'" % nameop['op']
        except Exception, e:
            log.exception(e)
            log.error("FATAL: unrecognized op '%s'" % nameop['op'] )
            sys.exit(1)

        if opcode in OPCODE_PREORDER_OPS:
            # preorder
            op_seq = self.commit_state_preorder( nameop, current_block_number )
            
        elif opcode in OPCODE_CREATION_OPS:
            # creation
            history_id_key = state_create_get_history_id_key( nameop )
            history_id = nameop[history_id_key]
            op_seq = self.commit_state_create( nameop, current_block_number )
           
        elif opcode in OPCODE_TRANSITION_OPS:
            # transition 
            history_id_key = state_transition_get_history_id_key( nameop )
            history_id = nameop[history_id_key]
            op_seq = self.commit_state_transition( nameop, current_block_number )
        
        else:
            raise Exception("Unknown operation '%s'" % opcode)

        if op_seq is None:
            log.error("FATAL: no op-sequence generated")
            sys.exit(1)

        if type(op_seq) != list:
            op_seq = [op_seq]

        # make sure all the mutate fields necessary to derive
        # the next consensus hash are in place.
        for i in xrange(0, len(op_seq)):

            cur = self.db.cursor()
            history = None 

            # temporarily store history...
            if history_id is not None:
                history = namedb_get_history( cur, history_id )
                op_seq[i]['history'] = history 

            # set all extra consensus fields 
            self.add_all_commit_consensus_values( opcode, op_seq[i], current_block_number )

            # revert...
            if history is not None:
                del op_seq[i]['history']

            self.log_commit( current_block_number, op_seq[i]['vtxindex'], op_seq[i]['op'], opcode, op_seq[i] )
    
        return op_seq


    def commit_state_preorder( self, nameop, current_block_number ):
        """
        Commit a state preorder (works for namespace_preorder,
        name_preorder, name_preorder_multi).

        DO NOT CALL THIS DIRECTLY
        """

        cur = self.db.cursor()

        # cannot have collided 
        if BlockstackDB.nameop_is_collided( nameop ):
            log.debug("Not commiting '%s', since it collided" % commit_preorder['preorder_hash'])
            self.log_reject( block_id, nameop['vtxindex'], nameop['op'], nameop )
            return []

        self.log_accept( current_block_number, nameop['vtxindex'], nameop['op'], nameop )

        commit_preorder = self.sanitize_op( nameop )
        rc = namedb_preorder_insert( cur, commit_preorder )
        if not rc:
            log.error("FATAL: failed to commit preorder '%s'" % commit_preorder['preorder_hash'] )
            sys.exit(1)

        return commit_preorder 


    def commit_state_create( self, nameop, current_block_number ):
        """
        Commit a state-creation operation (works for name_registration,
        namespace_reveal, name_registration_multi, name_import).

        DO NOT CALL THIS DIRECTLY
        """

        cur = self.db.cursor()
        opcode = nameop.get('opcode', None)

        try:
            assert state_create_is_valid( nameop ), "Invalid state-creation"

            preorder = state_create_get_preorder( nameop )
            prior_history_rec = state_create_get_prior_history( nameop )

            if prior_history_rec is not None:
               assert prior_history_is_valid( prior_history_rec ), "Invalid prior history"

            assert opcode is not None, "BUG: did not set opcode"
        except Exception, e:
            log.exception(e)
            log.error("FATAL: missing preorder and/or prior history and/or opcode")
            sys.exit(1)

        initial_state = self.sanitize_op( nameop )
        table = state_create_get_table( nameop )
        history_id_key = state_create_get_history_id_key( nameop )
        history_id = nameop[history_id_key]
        constraints_ignored = state_create_get_ignore_equality_constraints( nameop )

        # cannot have collided 
        if BlockstackDB.nameop_is_collided( nameop ):
            log.debug("Not commiting '%s' since we're collided" % history_id)
            self.log_reject( current_block_number, nameop['vtxindex'], nameop['op'], nameop )
            return []

        self.log_accept( current_block_number, nameop['vtxindex'], nameop['op'], nameop )

        if preorder is not None and prior_history_rec is not None:
            
            # re-ordered an expired piece of state   
            prior_block_number = prior_history_block_number( prior_history_rec )
            prior_record = None
            try:
                assert prior_block_number in prior_history_rec, "BUG: invalid prior history"
                prior_record_list = prior_history_rec[prior_block_number]

                assert len(prior_record_list) > 0, "BUG: missing prior history record"
                prior_record = prior_record_list[-1]

                assert 'preorder_hash' in preorder, "BUG: missing preorder_hash"
                assert history_id_key in prior_record, "BUG: '%s' not in prior history record" % history_id_key

            except Exception, e:
                log.exception(e)
                log.error("FATAL: invalid preorder or prior history")
                sys.exit(1)

            # create from prior history 
            rc = namedb_state_create_from_prior_history( cur, opcode, initial_state,
                                                         current_block_number, initial_state['vtxindex'], initial_state['txid'],
                                                         history_id, prior_history_rec, preorder, table )

            if not rc:
                log.error("FATAL: failed to create '%s' from prior history" % history_id )
                self.db.rollback()
                sys.exit(1)


            # clear the associated preorder 
            rc = namedb_preorder_remove( cur, preorder['preorder_hash'] )
            if not rc:
                log.error("FATAL: failed to remove preorder")
                sys.exit(1)


        elif preorder is not None:
            # create from preorder
            rc = namedb_state_create( cur, opcode, initial_state,
                                      current_block_number, initial_state['vtxindex'], initial_state['txid'],
                                      history_id, preorder, table )

            if not rc:
                log.error("FATAL: failed to create '%s' from preorder" % history_id )
                self.db.rollback()
                sys.exit(1)


        elif prior_history_rec is not None:
            # no preorder; this must be an import.
            # create from prior history.
            prior_block_number = prior_history_block_number( prior_history_rec )
            prior_record = None
            try:
                # must be an import 
                assert opcode in OPCODE_NAME_STATE_IMPORTS, "BUG: not an import operation"
                assert prior_block_number in prior_history_rec, "BUG: invalid prior history"
                prior_record_list = prior_history_rec[prior_block_number]

                assert len(prior_record_list) > 0, "BUG: missing prior history record"
                prior_record = prior_record_list[-1]

                assert history_id_key in prior_record, "BUG: '%s' not in prior history record" % history_id_key
                assert prior_record[history_id_key] == history_id, "BUG: prior history record is not for '%s'" % history_id
            except Exception, e:
                log.exception(e)
                log.error("FATAL: invalid prior history")
                sys.exit(1)

            rc = namedb_state_create_as_import( self.db, opcode, initial_state,
                                                current_block_number, initial_state['vtxindex'], initial_state['txid'],
                                                history_id, prior_record, table, constraints_ignored=constraints_ignored )

            if not rc:
                log.error("FATAL: failed to create '%s' from prior history" % history_id )
                self.db.rollback()
                sys.exit(1)

        else:
            # must be an import, and must be the first such for this name
            try:
                assert opcode in OPCODE_NAME_STATE_IMPORTS, "BUG: not an import operation"
            except Exception, e:
                log.exception(e)
                log.error("FATAL: invalid import operation")
                sys.exit(1)

            rc = namedb_state_create_as_import( self.db, opcode, initial_state, 
                                                current_block_number, initial_state['vtxindex'], initial_state['txid'],
                                                history_id, None, table, constraints_ignored=constraints_ignored )

            if not rc:
                log.error("FATAL: failed to create '%s' as initial import" % history_id)
                self.db.rollback()
                sys.exit(1)


        return initial_state


    def commit_state_transition( self, nameop, current_block_number ):
        """
        Commit a state transition (update, transfer, revoke, renew, namespace_ready).

        DO NOT CALL THIS DIRECTLY
        """

        cur = self.db.cursor()
        opcode = nameop.get('opcode', None)
        constraints_ignored = state_transition_get_ignore_equality_constraints( nameop )
        transition = self.sanitize_op( nameop )
        
        try:
            assert state_transition_is_valid( nameop ), "Invalid state-transition"
            assert opcode is not None, "No opcode given"
        except Exception, e:
            log.exception(e)
            log.error("FATAL: failed to commit state transition")
            self.db.rollback()
            sys.exit(1)

        table = state_transition_get_table( nameop )
        history_id_key = state_transition_get_history_id_key( nameop )
        history_id = nameop[history_id_key]

        # record must exist...
        if history_id_key == "name":
            cur_record = namedb_get_name( cur, history_id, current_block_number, include_history=False, include_expired=True )

        elif history_id_key == "namespace_id":
            cur_record = namedb_get_namespace( cur, history_id, current_block_number, include_history=False )

        else:
            raise Exception("Unknown history ID key '%s'" % history_id_key)

        try:
            assert cur_record is not None, "No such record: %s" % history_id
        except Exception, e:
            # should have been caught earlier
            log.exception(e)
            log.error("FATAL: failed to lookup existing record '%s'" % history_id)
            self.db.rollback()
            sys.exit(1)

        self.log_accept( current_block_number, nameop['vtxindex'], nameop['op'], nameop )

        rc = namedb_state_transition( cur, opcode, transition, current_block_number, transition['vtxindex'], transition['txid'],
                                      history_id, cur_record, table, constraints_ignored=constraints_ignored )
        if not rc:
            log.error("FATAL: failed to update '%s'" % history_id)
            self.db.rollback()
            sys.exit(1)

        new_record = None 
        if history_id_key == "name":
            new_record = namedb_get_name( cur, history_id, current_block_number, include_history=False )
        elif history_id_key == "namespace_id":
            new_record = namedb_get_namespace( cur, history_id, current_block_number, include_history=False )

        return new_record

    
    @classmethod
    def restore_from_history( cls, rec, block_id ):
        """
        Given a record with a history and a block number,
        calculate the sequence of states it went through
        in that block number.
        """
        return namedb_restore_from_history( rec, block_id )
       

    @classmethod 
    def get_previous_name_version( cls, name_rec, block_number, history_index, untrusted_db ):
        """
        Given a name record, a block number, and a history index, and a handle to an untrusted
        database, calculate the immediately previous version of this name just before (block_number, history_index).
        """
        
        untrusted_name_rec = untrusted_db.get_name( str(name_rec['name']) )
        name_rec['history'] = untrusted_name_rec['history']

        if history_index > 0:
            name_recs_prev = cls.restore_from_history( name_rec, block_number )
            assert history_index - 1 < len(name_recs_prev), "BUG: at %s: history_index - 1 = %s; >= %s" % (block_number, history_index - 1, len(name_recs_prev))
            
            name_rec_prev = name_recs_prev[ history_index - 1 ]
        else:
            name_recs_prev = cls.restore_from_history( name_rec, block_number - 1 )
            assert len(name_recs_prev) >= 1, "BUG: at %s: %s previous records" % (len(name_recs_prev))
            
            name_rec_prev = name_recs_prev[-1]

        del name_rec['history']
        return name_rec_prev


    @classmethod 
    def get_previous_namespace_version( cls, rec, block_number, history_index, untrusted_db ):
        """
        Given a name record, a block number, and a history index, and a handle to an untrusted
        database, calculate the immediately previous version of this name just before (block_number, history_index).
        """
        
        untrusted_namespace_rec = untrusted_db.get_namespace_by_preorder( str(rec['preorder_hash']) )
        rec['history'] = untrusted_namespace_rec['history']

        if history_index > 0:
            namespace_rec_prev = cls.restore_from_history( namespace_rec, block_number )[ history_index - 1 ]
        else:
            namespace_rec_prev = cls.restore_from_history( namespace_rec, block_number - 1 )[-1]

        del rec['history']
        return namespace_rec_prev

