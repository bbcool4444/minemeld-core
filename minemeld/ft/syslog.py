#  Copyright 2015 Palo Alto Networks, Inc
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import absolute_import

import logging
import gevent
import gevent.queue

import amqp
import ujson
import netaddr
import datetime
import socket

from . import base
from . import table

LOG = logging.getLogger(__name__)


class SyslogMatcher(base.BaseFT):
    def __init__(self, name, chassis, config):
        self.amqp_glet = None

        super(SyslogMatcher, self).__init__(name, chassis, config)

        self._ls_socket = None

    def configure(self):
        super(SyslogMatcher, self).configure()

        self.exchange = self.config.get('exchange', 'mmeld-syslog')
        self.rabbitmq_username = self.config.get('rabbitmq_username', 'guest')
        self.rabbitmq_password = self.config.get('rabbitmq_password', 'guest')

        self.input_types = self.config.get('input_types', {})

        self.logstash_host = self.config.get('logstash_host', None)
        self.logstash_port = self.config.get('logstash_port', 5514)

    def _initialize_tables(self, truncate=False):
        self.table_ipv4 = table.Table(self.name+'_ipv4', truncate=truncate)
        self.table_ipv4.create_index('_start')

        self.table_indicators = table.Table(
            self.name+'_indicators',
            truncate=truncate
        )

        self.table = table.Table(self.name, truncate=truncate)
        self.table.create_index('syslog_original_indicator')

    def initialize(self):
        self._initialize_tables()

    def rebuild(self):
        self._initialize_tables(truncate=True)

    def reset(self):
        self._initialize_tables(truncate=True)

    @base._counting('update.processed')
    def filtered_update(self, source=None, indicator=None, value=None):
        type_ = value.get('type', None)
        if type_ is None:
            LOG.error("%s - received update with no type, ignored", self.name)
            return

        itype = self.input_types.get(source, None)
        if itype is None:
            LOG.debug('%s - no type associated to %s, added %s',
                      self.name, source, type_)
            self.input_types[source] = type_
            itype = type_

        if itype != type_:
            LOG.error("%s - indicator of type %s received from "
                      "source %s with type %s, ignored",
                      self.name, type_, source, itype)
            return

        if type_ == 'IPv4':
            start, end = map(netaddr.IPAddress, indicator.split('-', 1))

            LOG.debug('start: %d', start.value)

            value['_start'] = start.value
            value['_end'] = end.value

            self.table_ipv4.put(indicator, value)

        else:
            self.table_indicators.put(type_+indicator, value)

    @base._counting('withdraw.processed')
    def filtered_withdraw(self, source=None, indicator=None, value=None):
        itype = self.input_types.get(source, None)
        if itype is None:
            LOG.error('%s - withdraw from unknown source', self.name)
            return

        if itype == 'IPv4':
            v = self.table_ipv4.get(indicator)
            if v is not None:
                self.table_ipv4.delete(indicator)

        else:
            v = self.table_indicators.get(itype+indicator)
            if v is not None:
                self.table_indicators.delete(itype+indicator)

        if v is not None:
            for i in self.table.query(index='syslog_original_indicator',
                                      from_key=itype+indicator,
                                      to_key=itype+indicator,
                                      include_value=False):
                self.emit_withdraw(i)
                self.table.delete(i)

    def _handle_ip(self, ip, source=True, message=None):
        try:
            ipv = netaddr.IPAddress(ip)
        except:
            return

        if ipv.version != 4:
            return

        ipv = ipv.value

        iv = next(
            (self.table_ipv4.query(index='_start',
                                   to_key=ipv,
                                   include_value=True,
                                   include_start=True,
                                   reverse=True)),
            None
        )
        if iv is None:
            return

        i, v = iv

        if v['_end'] < ipv:
            return

        for s in v.get('sources', []):
            self.statistics['source.'+s] += 1
        self.statistics['total_matches'] += 1

        v['syslog_original_indicator'] = 'IPv4'+i

        self.table.put(ip, v)
        self.emit_update(ip, v)

        if message is not None:
            self._send_logstash(
                message='matched IPv4',
                indicator=i,
                value=v,
                session=message
            )

    def _handle_url(self, url, message=None):
        domain = url.split('/', 1)[0]

        v = self.table_indicators.get('domain'+domain)
        if v is None:
            return

        v['syslog_original_indicator'] = 'domain'+domain

        for s in v.get('sources', []):
            self.statistics[s] += 1
        self.statistics['total_matches'] += 1

        self.table.put(domain, v)
        self.emit_update(domain, v)

        if message is not None:
            self._send_logstash(
                message='matched domain',
                indicator=domain,
                value=v,
                session=message
            )

    @base._counting('syslog.processed')
    def _handle_syslog_message(self, message):
        src_ip = message.get('src_ip', None)
        if src_ip is not None:
            self._handle_ip(src_ip, message=message)

        dst_ip = message.get('dest_ip', None)
        if dst_ip is not None:
            self._handle_ip(dst_ip, source=False, message=message)

        url = message.get('url', None)
        if url is not None:
            self._handle_url(url, message=message)

    def _amqp_callback(self, msg):
        try:
            message = ujson.loads(msg.body)
            self._handle_syslog_message(message)

        except gevent.GreenletExit:
            raise

        except:
            LOG.exception("%s - exception handling syslog message")

    def _amqp_consumer(self):
        while True:
            try:
                conn = amqp.connection.Connection(
                    userid=self.rabbitmq_username,
                    password=self.rabbitmq_password
                )
                channel = conn.channel()
                channel.exchange_declare(
                    self.exchange,
                    'fanout',
                    durable=False,
                    auto_delete=False
                )
                q = channel.queue_declare(
                    exclusive=False
                )

                channel.queue_bind(
                    queue=q.queue,
                    exchange=self.exchange,
                )
                channel.basic_consume(
                    callback=self._amqp_callback,
                    no_ack=True,
                    exclusive=True
                )

                while True:
                    conn.drain_events()

            except gevent.GreenletExit:
                break

            except:
                LOG.exception('%s - Exception in consumer glet', self.name)

            gevent.sleep(30)

    def _connect_logstash(self):
        if self._ls_socket is not None:
            return

        if self.logstash_host is None:
            return

        _ls_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _ls_socket.connect((self.logstash_host, self.logstash_port))

        self._ls_socket = _ls_socket

    def _send_logstash(self, message=None, indicator=None,
                       value=None, session=None):
        now = datetime.datetime.now()

        fields = {
            '@timestamp': now.isoformat()+'Z',
            '@version': 1,
            'syslog_node': self.name,
            'message': message
        }

        if indicator is not None:
            fields['indicator'] = indicator

        if value is not None:
            if 'last_seen' in value:
                last_seen = datetime.datetime.fromtimestamp(
                    float(value['last_seen'])/1000.0
                )
                value['last_seen'] = last_seen.isoformat()+'Z'

            if 'first_seen' in fields:
                first_seen = datetime.datetime.fromtimestamp(
                    float(value['first_seen'])/1000.0
                )
                value['first_seen'] = first_seen.isoformat()+'Z'

            fields['indicator_value'] = value

        if session is not None:
            session.pop('event.tags', None)
            fields['session'] = session

        try:
            self._connect_logstash()

            if self._ls_socket is not None:
                self._ls_socket.sendall(ujson.dumps(fields)+'\n')

        except:
            self._ls_socket = None
            raise

        self.statistics['logstash.sent'] += 1

    def mgmtbus_status(self):
        result = super(SyslogMatcher, self).mgmtbus_status()

        return result

    def length(self, source=None):
        return (self.table_ipv4.num_indicators +
                self.table_indicators.num_indicators)

    def start(self):
        super(SyslogMatcher, self).start()

        self.amqp_glet = gevent.spawn_later(
            2,
            self._amqp_consumer
        )

    def stop(self):
        super(SyslogMatcher, self).stop()

        if self.amqp_glet is None:
            return

class SyslogMiner(base.BaseFT):
    def __init__(self, name, chassis, config):
        self.amqp_glet = None
        self.ageout_glet = None

        self.active_requests = []
        self.rebuild_flag = False
        self.last_ageout_run = None

        self.state_lock = RWLock()

        super(SyslogMiner, self).__init__(name, chassis, config)

    def configure(self):
        super(SyslogMiner, self).configure()

        self.source_name = self.config.get('source_name', self.name)
        self.attributes = self.config.get('attributes', {})

        _age_out = self.config.get('age_out', {})

        self.age_out = {
            'interval': _age_out.get('interval', 3600),
            'default': _parse_age_out(_age_out.get('default', '30d'))
        }
        for k, v in _age_out.iteritems():
            if k in self.age_out:
                continue
            self.age_out[k] = _parse_age_out(v)

        self.exchange = self.config.get('exchange', 'mmeld-syslog')
        self.rabbitmq_username = self.config.get('rabbitmq_username', 'guest')
        self.rabbitmq_password = self.config.get('rabbitmq_password', 'guest')

    def _initialize_table(self, truncate=False):
        self.table = table.Table(self.name, truncate=truncate)
        self.table.create_index('_age_out')
        self.table.create_index('_withdrawn')

    def initialize(self):
        self._initialize_table()

    def rebuild(self):
        self.rebuild_flag = True
        self._initialize_table(truncate=(self.last_checkpoint is None))

    def reset(self):
        self._initialize_table(truncate=True)

    @base.BaseFT.state.setter
    def state(self, value):
        LOG.debug("%s - acquiring state write lock", self.name)
        self.state_lock.lock()
        #  this is weird ! from stackoverflow 10810369
        super(SyslogMiner, self.__class__).state.fset(self, value)
        self.state_lock.unlock()
        LOG.debug("%s - releasing state write lock", self.name)

    def _age_out_run(self):
        while True:
            self.state_lock.rlock()
            if self.state != ft_states.STARTED:
                self.state_lock.runlock()
                return

            try:
                now = utc_millisec()

                LOG.debug('now: %s', now)

                for i, v in self.table.query(index='_age_out',
                                             to_key=now-1,
                                             include_value=True):
                    LOG.debug('%s - %s %s aged out', self.name, i, v)

                    if v.get('_withdrawn', None) is not None:
                        continue

                    self.emit_withdraw(indicator=i)
                    v['_withdrawn'] = now
                    self.table.put(i, v)

                    self.statistics['aged_out'] += 1

                self.last_ageout_run = now

            except gevent.GreenletExit:
                break

            except:
                LOG.exception('Exception in _age_out_loop')

            finally:
                self.state_lock.runlock()

            try:
                gevent.sleep(self.age_out['interval'])
            except gevent.GreenletExit:
                break

    @base._counting('syslog.processed')
    def _handle_syslog_message(self, message):
        pass

    def _amqp_callback(self, msg):
        try:
            message = ujson.loads(msg.body)
            self._handle_syslog_message(message)

        except gevent.GreenletExit:
            raise

        except:
            LOG.exception("%s - exception handling syslog message")

    def _amqp_consumer(self):
        while self.last_ageout_run is None:
            gevent.sleep(1)

        self.state_lock.rlock()
        if self.state != ft_states.STARTED:
            self.state_lock.runlock()
            return

        try:
            if self.rebuild_flag:
                LOG.debug("rebuild flag set, resending current indicators")
                # reinit flag is set, emit update for all the known indicators
                for i, v in self.table.query(include_value=True):
                    type_, i = i.split('\0', 1)
                    self.emit_update(i, v)
        finally:
            self.state_lock.unlock()

        while True:
            try:
                conn = amqp.connection.Connection(
                    userid=self.rabbitmq_username,
                    password=self.rabbitmq_password
                )
                channel = conn.channel()
                channel.exchange_declare(
                    self.exchange,
                    'fanout',
                    durable=False,
                    auto_delete=False
                )
                q = channel.queue_declare(
                    exclusive=False
                )

                channel.queue_bind(
                    queue=q.queue,
                    exchange=self.exchange,
                )
                channel.basic_consume(
                    callback=self._amqp_callback,
                    no_ack=True,
                    exclusive=True
                )

                while True:
                    conn.drain_events()

            except gevent.GreenletExit:
                break

            except:
                LOG.exception('%s - Exception in consumer glet', self.name)

            gevent.sleep(30)

    def length(self, source=None):
        return self.table.num_indicators

    def start(self):
        super(SyslogMiner, self).start()

        if self.amqp_glet is not None:
            return

        self.amqp_glet = gevent.spawn_later(
            random.randint(0, 2),
            self._amqp_consumer
        )
        self.ageout_glet = gevent.spawn(self._age_out_run)

    def stop(self):
        super(SyslogMiner, self).stop()

        if self.amqp_glet is None:
            return

        for g in self.active_requests:
            g.kill()

        self.amqp_glet.kill()
        self.ageout_glet.kill()

        LOG.info("%s - # indicators: %d", self.name, self.table.num_indicators)
