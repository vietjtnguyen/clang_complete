from __future__ import print_function

from clang.cindex import *
import vim
import time
import threading
import os
import shlex
import importlib
import logging

from kinds import kinds

def getLogger(name):
    def get_loglevel():
        # logging setup
        level = logging.INFO
        if 'NVIM_PYTHON_LOG_LEVEL' in os.environ:
            l = getattr(logging,
                    os.environ['NVIM_PYTHON_LOG_LEVEL'].strip(),
                    level)
            if isinstance(l, int):
                level = l
        if 'NVIM_NCM_LOG_LEVEL' in os.environ:
            l = getattr(logging,
                    os.environ['NVIM_NCM_LOG_LEVEL'].strip(),
                    level)
            if isinstance(l, int):
                level = l
        return level
    logger = logging.getLogger(__name__)
    logger.setLevel(get_loglevel())
    return logger

logger = getLogger(__name__)

def decode(value):
  import sys
  if sys.version_info[0] == 2:
    return value

  try:
    return value.decode('utf-8')
  except AttributeError:
    return value

# Check if libclang is able to find the builtin include files.
#
# libclang sometimes fails to correctly locate its builtin include files. This
# happens especially if libclang is not installed at a standard location. This
# function checks if the builtin includes are available.
def canFindBuiltinHeaders(index, args = []):
  flags = 0
  currentFile = ("test.c", '#include "stddef.h"')
  try:
    tu = index.parse("test.c", args, [currentFile], flags)
  except TranslationUnitLoadError as e:
    return 0
  return len(tu.diagnostics) == 0

# Derive path to clang builtin headers.
#
# This function tries to derive a path to clang's builtin header files. We are
# just guessing, but the guess is very educated. In fact, we should be right
# for all manual installations (the ones where the builtin header path problem
# is very common) as well as a set of very common distributions.
def getBuiltinHeaderPath(library_path):
  if os.path.isfile(library_path):
    library_path = os.path.dirname(library_path)

  knownPaths = [
          library_path + "/../lib/clang",  # default value
          library_path + "/../clang",      # gentoo
          library_path + "/clang",         # opensuse
          library_path + "/",              # Google
          "/usr/lib64/clang",              # x86_64 (openSUSE, Fedora)
          "/usr/lib/clang"
  ]

  for path in knownPaths:
    try:
      subDirs = [f for f in os.listdir(path) if os.path.isdir(path + "/" + f)]
      subDirs = sorted(subDirs) or ['.']
      path = path + "/" + subDirs[-1] + "/include"
      if canFindBuiltinHeaders(index, ["-I" + path]):
        return path
    except:
      pass

  return None

def initClangComplete():

  global index

  clang_complete_flags = vim.eval('g:clang_complete_lib_flags')
  library_path = vim.eval('g:clang_library_path')
  clang_compilation_database = vim.eval('g:clang_compilation_database')

  debug = int(vim.eval("g:clang_debug")) == 1

  if library_path:
    if os.path.isdir(library_path):
      Config.set_library_path(library_path)
    else:
      Config.set_library_file(library_path)

  Config.set_compatibility_check(False)

  try:
    index = Index.create()
  except Exception as e:
    if library_path:
      suggestion = "Are you sure '%s' contains libclang?" % library_path
    else:
      suggestion = "Consider setting g:clang_library_path."

    if debug:
      exception_msg = str(e)
    else:
      exception_msg = ''

    logger.exception("Loading libclang failed, completion won't be available. %s %s ",
                     suggestion,
                     exception_msg)
    return 0

  global builtinHeaderPath
  builtinHeaderPath = None
  if not canFindBuiltinHeaders(index):
    builtinHeaderPath = getBuiltinHeaderPath(library_path)

    if not builtinHeaderPath:
      logger.warn("libclang find builtin header path failed: %s", builtinHeaderPath)

  global translationUnits
  translationUnits = dict()
  global complete_flags
  complete_flags = int(clang_complete_flags)
  global compilation_database
  if clang_compilation_database != '':
    compilation_database = CompilationDatabase.fromDirectory(clang_compilation_database)
  else:
    compilation_database = None
  global libclangLock
  libclangLock = threading.Lock()
  return 1


# Get a tuple (fileName, fileContent) for the file opened in the current
# vim buffer. The fileContent contains the unsafed buffer content.
def getCurrentFile():
  file = "\n".join(vim.current.buffer[:] + ["\n"])
  return (vim.current.buffer.name, file)


def getCurrentTranslationUnit(args, currentFile, fileName, update = False):
  tu = translationUnits.get(fileName)
  if tu != None:
    if update:
      tu.reparse([currentFile])
    return tu

  flags = TranslationUnit.PARSE_PRECOMPILED_PREAMBLE | \
          TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
  try:
    tu = index.parse(fileName, args, [currentFile], flags)
  except TranslationUnitLoadError as e:
    return None

  translationUnits[fileName] = tu

  # Reparse to initialize the PCH cache even for auto completion
  # This should be done by index.parse(), however it is not.
  # So we need to reparse ourselves.
  tu.reparse([currentFile])
  return tu

def splitOptions(options):
  # Use python's shell command lexer to correctly split the list of options in
  # accordance with the POSIX standard
  return shlex.split(options)

def getQuickFix(diagnostic):
  # Some diagnostics have no file, e.g. "too many errors emitted, stopping now"
  if diagnostic.location.file:
    filename = decode(diagnostic.location.file.name)
  else:
    filename = ""

  if diagnostic.severity == diagnostic.Ignored:
    type = 'I'
  elif diagnostic.severity == diagnostic.Note:
    type = 'I'
  elif diagnostic.severity == diagnostic.Warning:
    if "argument unused during compilation" in decode(diagnostic.spelling):
      return None
    type = 'W'
  elif diagnostic.severity == diagnostic.Error:
    type = 'E'
  elif diagnostic.severity == diagnostic.Fatal:
    type = 'E'
  else:
    return None

  return dict({ 'bufnr' : int(vim.eval("bufnr('" + filename + "', 1)")),
    'lnum' : diagnostic.location.line,
    'col' : diagnostic.location.column,
    'text' : decode(diagnostic.spelling),
    'type' : type})

def getQuickFixList(tu):
  return [_f for _f in map (getQuickFix, tu.diagnostics) if _f]

def highlightRange(range, hlGroup):
  pattern = '/\%' + str(range.start.line) + 'l' + '\%' \
      + str(range.start.column) + 'c' + '.*' \
      + '\%' + str(range.end.column) + 'c/'
  command = "exe 'syntax match' . ' " + hlGroup + ' ' + pattern + "'"
  vim.command(command)

def highlightDiagnostic(diagnostic):
  if diagnostic.severity == diagnostic.Warning:
    hlGroup = 'SpellLocal'
  elif diagnostic.severity == diagnostic.Error:
    hlGroup = 'SpellBad'
  else:
    return

  pattern = '/\%' + str(diagnostic.location.line) + 'l\%' \
      + str(diagnostic.location.column) + 'c./'
  command = "exe 'syntax match' . ' " + hlGroup + ' ' + pattern + "'"
  vim.command(command)

  for range in diagnostic.ranges:
    highlightRange(range, hlGroup)

def highlightDiagnostics(tu):
  for diagnostic in tu.diagnostics:
    highlightDiagnostic(diagnostic)

def highlightCurrentDiagnostics():
  if vim.current.buffer.name in translationUnits:
    highlightDiagnostics(translationUnits[vim.current.buffer.name])

def getCurrentQuickFixList():
  if vim.current.buffer.name in translationUnits:
    return getQuickFixList(translationUnits[vim.current.buffer.name])
  return []

# Get the compilation parameters from the compilation database for source
# 'fileName'. The parameters are returned as map with the following keys :
#
#   'args' : compiler arguments.
#            Compilation database returns the complete command line. We need
#            to filter at least the compiler invocation, the '-o' + output
#            file, the input file and the '-c' arguments. We alter -I paths
#            to make them absolute, so that we can launch clang from wherever
#            we are.
#            Note : we behave differently from cc_args.py which only keeps
#            '-I', '-D' and '-include' options.
#
#    'cwd' : the compiler working directory
#
# The last found args and cwd are remembered and reused whenever a file is
# not found in the compilation database. For example, this is the case for
# all headers. This achieve very good results in practice.
def getCompilationDBParams(fileName):
  if compilation_database:
    cmds = compilation_database.getCompileCommands(fileName)
    if cmds != None:
      cwd = decode(cmds[0].directory)
      args = []
      skip_next = 1 # Skip compiler invocation
      for arg in (decode(x) for x in cmds[0].arguments):
        if skip_next:
          skip_next = 0;
          continue
        if arg == '-c':
          continue
        if arg == fileName or \
           os.path.realpath(os.path.join(cwd, arg)) == fileName:
          continue
        if arg == '-o':
          skip_next = 1;
          continue
        if arg.startswith('-I'):
          includePath = arg[2:]
          if not os.path.isabs(includePath):
            includePath = os.path.normpath(os.path.join(cwd, includePath))
          args.append('-I'+includePath)
          continue
        args.append(arg)
      getCompilationDBParams.last_query = { 'args': args, 'cwd': cwd }

  # Do not directly return last_query, but make sure we return a deep copy.
  # Otherwise users of that result may accidently change it and store invalid
  # values in our cache.
  query = getCompilationDBParams.last_query
  return { 'args': list(query['args']), 'cwd': query['cwd']}

getCompilationDBParams.last_query = { 'args': [], 'cwd': None }

def getCompileParams(fileName,filetype=None):
  global builtinHeaderPath
  params = getCompilationDBParams(fileName)
  args = params['args']
  args += splitOptions(vim.eval("g:clang_user_options"))
  args += splitOptions(vim.eval("b:clang_user_options"))

  if filetype is None:
    filetype = vim.current.buffer.options['filetype']

  ftype_param = '-x c'

  if 'objc' in filetype:
    ftype_param = '-x objective-c'

  if filetype == 'cpp' or filetype == 'objcpp' or filetype[0:3] == 'cpp' or filetype[0:6] == 'objcpp':
    ftype_param += '++'

  _,ext = os.path.splitext(fileName)
  if 'h' in ext:
    ftype_param += '-header'

  args += splitOptions(ftype_param)

  if builtinHeaderPath:
    args.append("-I" + builtinHeaderPath)

  return { 'args' : args,
           'cwd' : params['cwd'] }

def updateCurrentDiagnostics():
  global debug
  debug = int(vim.eval("g:clang_debug")) == 1
  params = getCompileParams(vim.current.buffer.name)

  with libclangLock:
    getCurrentTranslationUnit(params['args'], getCurrentFile(),
                              vim.current.buffer.name, update = True)

def getCurrentCompletionResults(line, column, args, currentFile, fileName):

  tu = getCurrentTranslationUnit(args, currentFile, fileName)

  if tu == None:
    return None

  cr = tu.codeComplete(fileName, line, column, [currentFile],
      complete_flags)
  return cr

def formatResult(result):
  completion = dict()
  returnValue = None
  abbr = ""
  word = ""
  info = ""
  place_markers_for_optional_args = 0

  def roll_out_optional(chunks):
    result = []
    word = ""
    for chunk in chunks:
      if chunk.isKindInformative() or chunk.isKindResultType() or chunk.isKindTypedText():
        continue

      word += decode(chunk.spelling)
      if chunk.isKindOptional():
        result += roll_out_optional(chunk.string)

    return [word] + result

  for chunk in result.string:

    if chunk.isKindInformative():
      continue

    if chunk.isKindResultType():
      returnValue = chunk
      continue

    chunk_spelling = decode(chunk.spelling)

    if chunk.isKindTypedText():
      abbr = chunk_spelling

    if chunk.isKindOptional():
      for optional_arg in roll_out_optional(chunk.string):
        if place_markers_for_optional_args:
          word += ''
        info += optional_arg + "=?"

    if chunk.isKindPlaceHolder():
      word += ''
    else:
      word += chunk_spelling

    info += chunk_spelling

  menu = info

  if returnValue:
    menu = decode(returnValue.spelling) + " " + menu

  completion['word'] = abbr
  completion['abbr'] = abbr
  completion['menu'] = menu
  completion['info'] = info
  completion['dup'] = 1

  # Replace the number that represents a specific kind with a better
  # textual representation.
  completion['kind'] = kinds[result.cursorKind]

  return completion


def getAbbr(strings):
  for chunks in strings:
    if chunks.isKindTypedText():
      return decode(chunks.spelling)
  return ""

def jumpToLocation(filename, line, column, preview):
  filenameEscaped = decode(filename).replace(" ", "\\ ")
  if preview:
    command = "pedit +%d %s" % (line, filenameEscaped)
  elif filename != vim.current.buffer.name:
    command = "edit %s" % filenameEscaped
  else:
    command = "normal! m'"
  try:
    vim.command(command)
  except:
    # For some unknown reason, whenever an exception occurs in
    # vim.command, vim goes crazy and output tons of useless python
    # errors, catch those.
    return
  if not preview:
    vim.current.window.cursor = (line, column - 1)

def gotoDeclaration(preview=True):
  global debug
  debug = int(vim.eval("g:clang_debug")) == 1
  params = getCompileParams(vim.current.buffer.name)
  line, col = vim.current.window.cursor

  with libclangLock:
    tu = getCurrentTranslationUnit(params['args'], getCurrentFile(),
                                   vim.current.buffer.name,
                                   update = True)
    if tu is None:
      print("Couldn't get the TranslationUnit")
      return

    f = File.from_name(tu, vim.current.buffer.name)
    loc = SourceLocation.from_position(tu, f, line, col + 1)
    cursor = Cursor.from_location(tu, loc)
    defs = [cursor.get_definition(), cursor.referenced]

    for d in defs:
      if d is not None and loc != d.location:
        loc = d.location
        if loc.file is not None:
          jumpToLocation(loc.file.name, loc.line, loc.column, preview)
        break

# vim: set ts=2 sts=2 sw=2 expandtab :
