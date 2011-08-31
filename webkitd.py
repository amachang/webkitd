# -*- coding: utf-8 -*

from __future__ import with_statement

from pprint import pprint as p
import signal, json, sys, os, re, traceback, uuid, json, time, errno

from PyQt4.QtCore import *
from PyQt4.QtGui import *
from PyQt4.QtWebKit import *
from PyQt4.QtNetwork import *


class WebKitServer(QTcpServer):


  @classmethod
  def walkObjectTree(cls, qobj, callback):
    children = map(lambda(c): cls.walkObjectTree(c, callback), qobj.children())
    return callback(qobj, children)


  @classmethod
  def getObjectTree(cls, qobj):
    def getObjectTreeInternal(qobj, children):
      return {
        u'type': unicode(qobj.metaObject().className()),
        u'children': children
      }
    return cls.walkObjectTree(qobj, getObjectTreeInternal)


  @classmethod
  def getObjectCount(cls, qobj):
    def getObjectCountInternal(qobj, children):
      return reduce(lambda x, y: x + y, children, 0) + 1
    return cls.walkObjectTree(qobj, getObjectCountInternal)


  @classmethod
  def getObjectCountMap(cls, qobj):
    countMap = {}
    def getObjectCountInternal(qobj, children):
      className = unicode(qobj.metaObject().className());
      if countMap.has_key(className):
        countMap[className] = countMap[className] + 1
      else:
        countMap[className] = 1
    cls.walkObjectTree(qobj, getObjectCountInternal)
    return countMap


  @classmethod
  def init(cls):
    signal.signal(signal.SIGINT, signal.SIG_DFL)



  @classmethod
  def start(cls, host='127.0.0.1', port=1982):
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    cls.init()
    app = QApplication([])
    cls.app = app
    server = cls(app)

    if not server.listen(QHostAddress(host), port):
      raise Exception(u'Cannot listen!')

    return app.exec_()


  @classmethod
  def daemon(cls, op='start', host=u'127.0.0.1', port=1982, pidPath=u'/tmp/webkitd.pid', stdout=u'/tmp/webkitd.out', stderr=u'/tmp/webkitd.err'):
    from lockfile.pidlockfile import PIDLockFile

    pidFile = PIDLockFile(pidPath)

    def isOld(pidFile):
      old = False
      try:
        os.kill(pidFile.read_pid(), signal.SIG_DFL)
      except OSError, e:
        if e.errno == errno.ESRCH:
          old = True
      return old

    if op == u'start':
      if pidFile.is_locked():
        if isOld(pidFile):
          pidFile.break_lock()
        else:
          print u'Daemon is still started.'
          return

      if sys.platform == 'win32':
        cls.start(host, port)
      else:
        from daemon import DaemonContext

        dc = DaemonContext(
          pidfile=pidFile,
          stderr=open(stderr, u'w+'),
          stdout=open(stdout, u'w+')
        )

        with dc:
          cls.start(host, port)

    elif op == u'stop':
      if not pidFile.is_locked():
        print u'Daemon is not started yet.'
        return

      if isOld(pidFile):
        pidFile.break_lock()
      else:
        os.kill(pidFile.read_pid(), signal.SIGINT)

    else:
      raise Exception(u'Unsupported daemon operation.')


  def __init__(self, app):
    self.app = app
    QTcpServer.__init__(self, app)
    self.newConnection.connect(self.handleNewConnection)


  def handleNewConnection(self):
    while self.hasPendingConnections():
      socket = self.nextPendingConnection()

      if socket == None:
        return

      socket.disconnected.connect(socket.deleteLater)

      worker = self.__class__.Worker(socket, self.app, self.__class__.Page)


class WebKitWorker(QObject):


  jobClassMap = {}


  def __init__(self, socket, app, Page):
    print u'New worker created: ' + unicode(socket.peerAddress().toString()) + u':' + unicode(socket.peerPort())
    QObject.__init__(self, app)
    self.app = app
    self.Page = Page
    self.page = Page(app)
    self.socket = socket
    self.isBusy = False
    self.page.javascriptInterrupted.connect(self.handleJavaScriptInterrupted)
    socket.disconnected.connect(self.page.deleteLater)
    socket.disconnected.connect(self.deleteLater)
    socket.readyRead.connect(self.handleReadyRead)


  def handleJavaScriptInterrupted(self):
    self.fatalAndDisconnect(Exception(u'Heavy JavaScript interrupted!'))


  def sendData(self, data):
    self.socket.write((data + u'\n').encode(u'UTF-8'))


  def handleReadyRead(self):
    print u'Handle Ready Read'
    while self.socket and self.socket.canReadLine():
      # For UTF-8 Text Protocol
      # QByteArray -> Python String -> Python Unicode
      line = self.socket.readLine();
      data = str(line).decode(u'UTF-8').strip()
      print u'Data arrived: ' + data
      self.emitData(data)


  def emitData(self, data):
    try:
      if not self.isBusy:
        self.isBusy = True
        try:
          data = json.loads(data, encoding=u'UTF-8')
        except ValueError, e:
          raise WebKitProtocolViolationException(e)

        if not data.has_key(u'type') or not data.has_key(u'data'):
          raise WebKitProtocolViolationException(u'Invalid sended data. Data must has \'type\' and \'data\' key.')

        t = data[u'type']

        if not self.jobClassMap.has_key(t):
          raise WebKitProtocolViolationException(u'Invalid sended data. Invalid job: ' + t)

        Job = self.__class__.jobClassMap[t]
        try:
          job = Job(self, self.page, data[u'data'])
        except Exception, e:
          raise Exception(u'Can\'t create job for ' + t + u'.')
          
        job.start()

      else:
        raise WebKitProtocolViolationException(u'WebKit worker is still processing.')

    except Exception, e:
      traceback.print_exc();
      self.fatal(e)


  def fatalAndDisconnect(self, e):
    self.sendData(json.dumps({ u'error': unicode(e), u'fatal': True, u'disconnect': True }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False
    self.disconnect()


  def fatal(self, e):
    self.sendData(json.dumps({ u'error': unicode(e), u'fatal': True }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def error(self, e):
    self.sendData(json.dumps({ u'error': unicode(e) }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def finish(self, data):
    self.sendData(json.dumps({ u'data': data }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def disconnect(self):
    print u'Worker destroyed.'
    self.socket.disconnectFromHost()
    self.socket.readyRead.disconnect(self.handleReadyRead)
    self.page.settings().clearMemoryCaches()
    self.socket = None
    self.page = None


  @classmethod
  def appendJobClass(cls, JobClass):
    cls.jobClassMap[JobClass.id] = JobClass



class WebKitLoadUrlJob():


  id = u'load-url'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.url = data[u'url']


  def start(self):
    try:
      self.page.navigate(self.url, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, ok, status, timeouted):
    try:
      title = unicode(self.page.mainFrame().findFirstElement(u'title').toPlainText())
      finalUrl = unicode(self.page.mainFrame().url().toString())
      url = unicode(self.page.mainFrame().requestedUrl().toString())
      self.worker.finish({ u'ok': ok, u'status': status, u'timeouted': timeouted, u'title': title, u'finalUrl': finalUrl, u'url': url })
    except Exception, e:
      traceback.print_exc()
      self.worker.error(e)



class WebKitCheckElementJob():


  id = u'check-element'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']


  def start(self):
    try:
      self.page.checkElement(self.xpath, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, timeouted, error):
    try:
      self.worker.finish({ u'found': found, u'count': count, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitClickElementJob(WebKitCheckElementJob):


  id = u'click-element'


  def start(self):
    try:
      self.page.click(self.xpath, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitWaitElementJob(WebKitCheckElementJob):


  id = u'wait-element'


  def start(self):
    self.startTime = time.time()
    self.check()


  def check(self):
    try:
      timeout =  self.startTime + self.timeout - time.time()
      if timeout < 0:
        self.worker.finish({ u'found': False, u'count': 0, u'error': u'Timeouted!', u'timeouted': True})
      else:
        self.page.checkElement(self.xpath, timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, timeouted, error):
    try:
      if (found or error):
        self.worker.finish({ u'found': found, u'count': count, u'error': error, u'timeouted': timeouted})
      else:
        self.page.timeout(1, self.check)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitScrollElementJob():


  id = u'scroll-element'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.scroll = data[u'scroll']


  def start(self):
    try:
      self.page.scrollElement(self.xpath, self.scroll, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, beforeTop, afterTop, scrollHeight, timeouted, error):
    try:
      self.worker.finish({
        u'found': found,
        u'count': count,
        u'beforeTop': beforeTop,
        u'afterTop': afterTop,
        u'scrollHeight': scrollHeight,
        u'timeouted': timeouted,
        u'error': error
      })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitGetElementValueJob():


  id = u'get-element-value'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.attr = data[u'attr']


  def start(self):
    try:
      self.page.getElementValue(self.xpath, self.attr, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, hasProp, propValue, hasAttr, attrValue, timeouted, error):
    try:
      self.worker.finish({
        u'found': found,
        u'count': count,
        u'hasProp': hasProp,
        u'propValue': propValue,
        u'hasAttr': hasAttr,
        u'attrValue': attrValue,
        u'timeouted': timeouted,
        u'error': error,
      })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitSetElementAttrJob(WebKitCheckElementJob):


  id = u'set-element-attr'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.attr = data[u'attr']
    self.value = data[u'value']


  def start(self):
    try:
      self.page.setElementAttribute(self.xpath, self.attr, self.value, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitSetElementPropJob(WebKitCheckElementJob):


  id = u'set-element-prop'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.prop = data[u'prop']
    self.value = data[u'value']


  def start(self):
    try:
      self.page.setElementProperty(self.xpath, self.prop, self.value, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitCallElementMethodJob():


  id = u'check-element-method'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.prop = data[u'prop']
    self.args = data[u'args']


  def start(self):
    try:
      self.page.callElementMethod(self.xpath, self.prop, self.args, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, resultList, timeouted, error):
    try:
      self.worker.finish({ u'found': found, u'count': count, u'resultList': resultList, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitEvalStringJob():


  id = u'eval-string'


  def __init__(self, worker, page, data):
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.code = data[u'code']


  def start(self):
    try:
      self.page.evalString(self.code, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, result, timeouted, error):
    try:
      self.worker.finish({ u'result': result, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitServerJob():


  id = u'server'


  def __init__(self, worker, page, data):
    self.data = data
    self.worker = worker
    self.page = page;


  def start(self):
    try:
      if (self.data[u'command'] == u'disconnect'):
        self.worker.finish({ u'message': 'bye-bye' })
        self.worker.disconnect()
      elif (self.data[u'command'] == u'status'):
        
        objectCount = WebKitServer.getObjectCount(self.worker.app)
        objectCountMap = WebKitServer.getObjectCountMap(self.worker.app)

        status = self.page.status()
        self.worker.finish({
          u'objectCount': objectCount,
          u'objectCountMap': objectCountMap,
          u'title': status[u'title'],
          u'url': status[u'url'],
          u'requestedUrl': status[u'requestedUrl'],
          u'totalBytes': status[u'totalBytes'],
          u'width': status[u'width'],
          u'height': status[u'height'],
          u'selectedText': status[u'selectedText']
        })
      else:
        self.worker.finish({ u'error': 'command not found' })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitPage(QWebPage):

  javascriptInterrupted = pyqtSignal()

  def __init__(self, parent):
    QWebPage.__init__(self, parent)

    self.timer = QTimer()
    self.timer.isUsed = False
    self.timer.setSingleShot(True)

    settings = {
      QWebSettings.AutoLoadImages: False,
      QWebSettings.JavascriptCanOpenWindows: False,
      QWebSettings.JavaEnabled: False,
      QWebSettings.PluginsEnabled: False,
      QWebSettings.PrivateBrowsingEnabled: True,
      QWebSettings.DnsPrefetchEnabled: True,
    }

    for k, v in settings.items():
      self.settings().setAttribute(k, v)

    self.loadProgress.connect(self.handleLoadProgress)
    self.loadStarted.connect(self.handleLoadStarted)
    self.loadFinished.connect(self.handleLoadFinished)
    self.networkAccessManager().finished.connect(self.handleResourceLoadFinished)
    self.mainFrame().initialLayoutCompleted.connect(self.handleInitialLayoutCompleted)
    self.mainFrame().javaScriptWindowObjectCleared.connect(self.handleJavaScriptWindowObjectCleared)
    self.mainFrame().urlChanged.connect(self.handleUrlChanged)
    self.mainFrame().titleChanged.connect(self.handleTitleChanged)
    self.mainFrame().pageChanged.connect(self.handlePageChanged)

    self.setViewportSize(QSize(400, 300))


  def status(self):
    return {
      u'title': unicode(self.mainFrame().findFirstElement(u'title').toPlainText()),
      u'url': unicode(self.mainFrame().url().toString()),
      u'requestedUrl': unicode(self.mainFrame().requestedUrl().toString()),
      u'totalBytes': self.totalBytes(),
      u'width': self.mainFrame().contentsSize().width(),
      u'height': self.mainFrame().contentsSize().height(),
      u'selectedText': unicode(self.selectedText()),
    }


  def handleInitialLayoutCompleted(self):
    print u'Handle initial layout completed.'


  def handleInitialLayoutCompleted(self):
    print u'Handle initial layout completed.'


  def handleJavaScriptWindowObjectCleared(self):
    print u'JavaScript window object cleared.'

 
  def handleTitleChanged(self, title):
    print u'Title changed: ' + unicode(title)


  def handleUrlChanged(self, url):
    print u'Url changed: ' + url.toString()


  def handlePageChanged(self):
    print u'Page changed.'

  
  def handleLoadProgress(self, progress):
    print u'Loading... ' + str(progress) + u'%'


  def handleLoadStarted(self):
    print u'Load started.'


  def handleLoadFinished(self, ok):
    print u'Load finished.'


  def handleResourceLoadFinished(self, reply):
    print u'Resource loaded: ' + unicode(reply.url().toString())


  def timeout(self, time, callback):
    print u'Waiting: ' + unicode(time) + u' sec'
    if (self.timer.isUsed):
      raise Exception(u'Timer is used')
    self.timer.isUsed = True
    self.timer.start(time * 1000)

    def handleTimeout():
      self.timer.stop()
      self.timer.timeout.disconnect(handleTimeout)
      self.timer.isUsed = False
      callback()

    self.timer.timeout.connect(handleTimeout)


  def cancelTimeout(self):
    self.timer.isUsed = False
    self.timer.stop()


  def navigate(self, url, timeout, callback):
    print u"Preparing to load...: " + url

    context = {
      u'status': 0,
      u'timeouted': False,
      u'requestArrived': False,
      u'nam': self.networkAccessManager()
    }

    def handleResourceLoadFinished(reply):
      if (context[u'timeouted']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      if (unicode(self.mainFrame().url().toString()) == unicode(reply.url().toString())):
        context[u'status'] = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute).toInt()[0]

    def handleLoadFinished(ok):
      self.loadFinished.disconnect(handleLoadFinished)
      context[u'nam'].finished.disconnect(handleResourceLoadFinished)
      self.cancelTimeout()
      context[u'requestArrived'] = True
      if (context[u'timeouted']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      callback(ok, context[u'status'], context[u'timeouted'])

    def handleTimeout():
      self.loadFinished.disconnect(handleLoadFinished)
      context[u'nam'].finished.disconnect(handleResourceLoadFinished)
      self.cancelTimeout()
      context[u'timeouted'] = True
      if (context[u'requestArrived']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      callback(False, context[u'status'], context[u'timeouted'])

    self.timeout(timeout, handleTimeout)
    self.mainFrame().load(QUrl(url))
    self.loadFinished.connect(handleLoadFinished)
    context[u'nam'].finished.connect(handleResourceLoadFinished)


  def click(self, xpath, timeout, callback):
    print u'Preparing to click...: ' + xpath
    js = '''
      function(xpath) {
        var elList = __xpath__(xpath);
        elList.forEach(__click__);
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def setElementAttribute(self, xpath, attr, value, timeout, callback):
    print u'Preparing to set element attribute...: ' + xpath + u' (' + attr + u')'
    js = '''
      function(xpath, attr, value) {
        var elList = __xpath__(xpath);
        elList.forEach(__setAttr__(attr, value))
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [attr, value], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def setElementProperty(self, xpath, prop, value, timeout, callback):
    print u'Preparing to set element property...: ' + xpath + u' (' + prop + u')'
    js = '''
      function(xpath, prop, value) {
        var elList = __xpath__(xpath);
        elList.forEach(__setProp__(prop, value))
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [prop, value], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def callElementMethod(self, xpath, prop, args, timeout, callback):
    print u'Preparing to call element method...: ' + xpath + u' (' + prop + u')'
    js = '''
      function(xpath, prop, args) {
        var elList = __xpath__(xpath);
        var resultList = elList.map(__callProp__(prop, args))
        return [!!elList.length, elList.length, resultList]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    resultList = []
    try:
      (found, count, resultList) = self.js(js, [prop, args], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, resultList, timeouted, error)


  def evalString(self, code, timeout, callback):
    print u'Preparing to evaluate string...: (' + code + u')'
    js = '''
      function(code) {
        return eval(code)
      }
    '''
    result = False
    timeouted = False
    error = False
    try:
      result = self.js(js, [code], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(result, timeouted, error)


  def getElementValue(self, xpath, attr, timeout, callback):
    print u'Preparing to get element value...: ' + xpath + u' (' + attr + u')'
    js = '''
      function(xpath, attr) {
        var elList = __xpath__(xpath);
        var el = elList[0];
        if (el) {
          var hasProp = attr in el;
          var propValue = el[attr];
          var hasAttr =  el.hasAttribute(attr);
          var attrValue = el.getAttribute(attr);
        }
        else {
          var hasProp = false;
          var propValue = void(0);
          var hasAttr = false;
          var attrValue = void(0);
        }
        return [!!elList.length, elList.length, hasProp, propValue, hasAttr, attrValue]
      }
    '''
    found = False
    count = 0
    hasProp = False
    propValue = None
    hasAttr = False
    attrValue = None
    timeouted = False
    error = False
    try:
      (found, count, hasProp, propValue, hasAttr, attrValue) = self.js(js, [xpath, attr], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, hasProp, propValue, hasAttr, attrValue, timeouted, error)


  def scrollElement(self, xpath, scroll, timeout, callback):
    print u'Preparing to scroll...: ' + xpath + u' (' + unicode(scroll) + u')'
    js = '''
      function(xpath, scroll) {
        var elList = __xpath__(xpath);
        var el = elList[0];
        if (el) {
          var r = __scroll__(scroll)(el)
          var beforeTop = r[0];
          var afterTop = r[1];
          var scrollHeight = r[2];
        }
        else {
          var beforeTop = 0, afterTop = 0, scrollHeight = 0;
        }
        return [!!elList.length, elList.length, beforeTop, afterTop, scrollHeight]
      }
    '''
    found = False
    count = 0
    beforeTop = 0
    afterTop = 0
    scrollHeight = 0
    timeouted = False
    error = False
    try:
      (found, count, beforeTop, afterTop, scrollHeight) = self.js(js, [xpath, scroll], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, beforeTop, afterTop, scrollHeight, timeouted, error)


  def checkElement(self, xpath, timeout, callback):
    print u'Preparing to check...: ' + xpath
    js = '''
      function(xpath) {
        var elList = __xpath__(xpath);
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def js(self, js, args, timeout):
    timer = QTimer()
    def handleTimeout():
      print "Heavy JavaScript interrupted!"
      self.javascriptInterrupted.emit()
    timer.timeout.connect(handleTimeout)
    timer.setSingleShot(True)
    timer.start(1)
    frame = self.mainFrame()
    result = frame.evaluateJavaScript(u'''(function() {
      var __args__ = ''' + json.dumps(args, ensure_ascii=False, encoding=u'UTF-8') + u''';
      var __timeout__ = ''' + unicode(timeout * 1000) + u''';
      var __startTime__ = new Date().getTime();
      var __logs__ = [];
      var __isTimeouted__ = false;
      function __click__(element) {
        __checkTimeout__();
        var e = document.createEvent('MouseEvents');
        __checkTimeout__();
        e.initMouseEvent("click", true, true, window,
            0, 0, 0, 0, 0, false, false, false, false, 0, null);
        __checkTimeout__();
        var r = element.dispatchEvent(e);
        __checkTimeout__();
        return r;
      }
      function __callProp__(prop, args) {
        __checkTimeout__();
        return function(obj) {
          __checkTimeout__();
          return obj[prop].apply(obj, args)
        }
      }
      function __setAttr__(attr, value) {
        __checkTimeout__();
        return function(element) {
          __checkTimeout__();
          element.setAttribute(attr, value)
        }
      }
      function __setProp__(prop, value) {
        __checkTimeout__();
        return function(obj) {
          __checkTimeout__();
          obj[prop] = value;
        }
      }
      function __scroll__(scroll) {
        __checkTimeout__();
        return function(element) {
          __checkTimeout__();
          var beforeTop = element.scrollTop;
          __checkTimeout__();
          element.scrollTop = scroll;
          __checkTimeout__();
          var afterTop = element.scrollTop;
          __checkTimeout__();
          var scrollHeight = element.scrollHeight;
          __checkTimeout__();
          return [beforeTop, afterTop, scrollHeight];
        }
      }
      function __log__(message) {
        __checkTimeout__();
        __logs__.push(message.toString());
      }
      function __xpath__(xpath, context, fn) {
        __checkTimeout__();
        context = context || document;
        __checkTimeout__();
        fn = fn || function(node) { return node };
        __checkTimeout__();
        var r = document.evaluate(xpath, context, null, 7, null);
        __checkTimeout__();
        var list = [];
        __checkTimeout__();
        for (var i = 0; i < r.snapshotLength; i++) {
          __checkTimeout__();
          var result = fn(r.snapshotItem(i))
          __checkTimeout__();
          list.push(result);
        }
        __checkTimeout__();
        var resultList = list.filter(function(e) { return e });
        __checkTimeout__();
        return resultList;
      }
      function __checkTimeout__() {
        if (new Date().getTime() - __startTime__ > __timeout__) {
          __isTimeouted__ = true;
          throw Error('Timeout in JavaScript.')
        }
      }
      // for webkit evaluateJavaScript problem
      function __cleanUpNull__(data) {
        for (name in data) {
          if (data[name] === null || data[name] === void(0)) {
            data[name] = false;
            if (typeof (data[name]) === 'object' && data[name] !== null) {
              __cleanUpNull__(data[name]);
            }
          }
        }
      }
      try {
        var __result__ = (''' + js + u''' ).apply(null, __args__)
        __cleanUpNull__(__result__)
        return { data: __result__, timeouted: __isTimeouted__, logs: __logs__ };
      }
      catch (e) {
        return { error: e.message, timeouted: __isTimeouted__, logs: __logs__ };
      }
    })()''')
    timer.stop()
    result = self.toPythonValue(result)
    for log in result[u'logs']:
      print u'JavaScript Log: ' + log
    if result.has_key(u'error') and result[u'timeouted']:
      raise WebKitJavaScriptTimeoutException(result[u'error'])
    if result.has_key(u'error') and not result[u'timeouted']:
      raise WebKitJavaScriptErrorException(result[u'error'])
    if result[u'timeouted']:
      raise Exception(u'Timeout and JavaScript success is occurred both. This is bug.')
    if not result.has_key(u'data'):
      raise Exception(u'JavaScript result dosen\'t have data. This is bug.')

    return result[u'data']


  def toPythonValue(self, qtValue):
    if (qtValue.typeName() == u'QString'):
      value = unicode(qtValue.toString())
    elif (qtValue.typeName() == u'double'):
      value = qtValue.toDouble()[0]
    elif (qtValue.typeName() == u'bool'):
      value = qtValue.toBool()
    elif (qtValue.typeName() == u'QVariantList'):
      value = []
      for v in qtValue.toList():
        value.append(self.toPythonValue(v))
    elif (qtValue.typeName() == u'QVariantMap'):
      value = {}
      qtMap = qtValue.toMap()
      for k, v in qtMap.items():
        value[unicode(k)] = self.toPythonValue(v)
    else:
      raise Exception(u'Unsupported QtType: ' + qtValue.typeName())
    return value


  # XXX 現状の libwebkit ではこれは non-virtual なため効かない
  @pyqtSlot(bool)
  def shouldInterruptJavascript():
    return False


  def chooseFile(self, frame, suggestedFile):
    return u''


  def javascriptAlert(self, frame, msg):
    pass


  def javascriptPrompt(self, frame, msg):
    return True


  def javascriptConfirm(self, frame, msg):
    return True


  def javascriptConsoleMessage(self, msg, lineNumber, sourceId):
    pass


  def userAgentForUrl(self, url):
    return u'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/534.30 (KHTML, like Gecko) Chrome/12.0.742.112 Safari/534.30'


  def supportsExtension(self, extension):
    return extension == QWebPage.ErrorPageExtension


  def extension(self, extension, option, output):
    if (extension == QWebPage.ErrorPageExtension):
      print u'Page error has ocurred!'
    return True
  


class WebKitProtocolViolationException(Exception):
  pass

class WebKitJavaScriptTimeoutException(Exception):
  pass

class WebKitJavaScriptErrorException(Exception):
  pass



WebKitServer.Worker = WebKitWorker
WebKitServer.Page = WebKitPage

WebKitWorker.appendJobClass(WebKitLoadUrlJob)
WebKitWorker.appendJobClass(WebKitServerJob)
WebKitWorker.appendJobClass(WebKitClickElementJob)
WebKitWorker.appendJobClass(WebKitCheckElementJob)
WebKitWorker.appendJobClass(WebKitWaitElementJob)
WebKitWorker.appendJobClass(WebKitScrollElementJob)
WebKitWorker.appendJobClass(WebKitGetElementValueJob)
WebKitWorker.appendJobClass(WebKitSetElementAttrJob)
WebKitWorker.appendJobClass(WebKitSetElementPropJob)
WebKitWorker.appendJobClass(WebKitCallElementMethodJob)
WebKitWorker.appendJobClass(WebKitEvalStringJob)

if __name__ == "__main__":

  import argparse
  parser = argparse.ArgumentParser(description=u'Simple WebKit Server')
  subparsers = parser.add_subparsers(help='operation')
  startparser = subparsers.add_parser('start', help='Start server')
  startparser.add_argument(u'--host', default='127.0.0.1')
  startparser.add_argument(u'--port', type=int, default=1982)
  startparser.add_argument(u'--pidfile', default='/tmp/webkitd.pid')
  startparser.add_argument(u'--stdout', default='/tmp/webkitd.out')
  startparser.add_argument(u'--stderr', default='/tmp/webkitd.err')
  stopparser = subparsers.add_parser('stop', help='Stop server')

  args = parser.parse_args()

  if sys.argv[1] == 'start':
    WebKitServer.daemon(sys.argv[1], args.host, args.port, args.pidfile, args.stdout, args.stderr)
  else:
    WebKitServer.daemon(sys.argv[1])

