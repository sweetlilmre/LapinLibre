// SPDX-License-Identifier: MIT
/*
 * SerialStream / SerialStreamBuf: see serial-stream.h.
 */
#include "serial-stream.h"

#include <cstring>

#include <fcntl.h>
#include <unistd.h>

/* Software flow control characters. */
#define XON 0x11  /* ^Q */
#define XOFF 0x13 /* ^S */

using namespace serial;

SerialStream::SerialStream(const std::string &name, speed_t baud, int char_size, Parity parity, short stop_bits,
                           FlowControl flow_control)
    : fd_(-1), putback_available_(false), putback_char_(0), state_(0)
{
    open(name);
    /* SerialStream::Set*(): each runs even when the port failed to open. */
    if (set_baud_rate(baud) == BAUD_INVALID)
        setstate(BADBIT);
    if (set_char_size(char_size) == CHAR_SIZE_INVALID)
        setstate(BADBIT);
    if (set_parity(parity) == PARITY_INVALID)
        setstate(BADBIT);
    /* The stop bits setter reports failure as 0. */
    if (set_num_of_stop_bits(stop_bits) == 0)
        setstate(BADBIT);
    if (set_flow_control(flow_control) == FLOW_CONTROL_INVALID)
        setstate(BADBIT);
}

SerialStream::~SerialStream()
{
    if (fd_ != -1)
        buf_close();
}

void SerialStream::open(const std::string &name)
{
    if (!buf_open(name))
        setstate(BADBIT);
}

/* SerialStreamBuf::open(name, ios::in | ios::out) */
bool SerialStream::buf_open(const std::string &name)
{
    if (fd_ != -1)
        return false;
    fd_ = ::open(name.c_str(), O_RDWR | O_NOCTTY);
    if (fd_ == -1)
        return false;
    /* On failure the descriptor stays open (and non-blocking). */
    return initialize_serial_port() != -1;
}

bool SerialStream::buf_close()
{
    if (fd_ == -1)
        return false;
    if (::close(fd_) == -1)
        return false;
    fd_ = -1;
    return true;
}

int SerialStream::initialize_serial_port()
{
    int flags;

    if (fd_ == -1)
        return -1;
    /* Non-blocking while the port is reset, so a modem line can't stall us. */
    flags = fcntl(fd_, F_GETFL, 0);
    if (fcntl(fd_, F_SETFL, flags | O_NONBLOCK) == -1)
        return -1;
    if (tcflush(fd_, TCIOFLUSH) == -1)
        return -1;
    if (set_parameters_to_default() == -1)
        return -1;
    flags = fcntl(fd_, F_GETFL, 0);
    if (fcntl(fd_, F_SETFL, flags & ~O_NONBLOCK) == -1)
        return -1;
    return 0;
}

int SerialStream::set_parameters_to_default()
{
    struct termios t;

    if (fd_ == -1)
        return -1;
    /* Raw 8N1 at 19200 baud, receiver on, modem lines ignored, break ignored;
     * reads return after one byte. */
    std::memset(&t, 0, sizeof t);
    t.c_iflag = IGNBRK;
    t.c_cflag = CLOCAL | CREAD | CS8 | B19200;
    t.c_cc[VTIME] = 0;
    t.c_cc[VMIN] = 1;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return -1;
    if (set_baud_rate(BAUD_DEFAULT) == BAUD_INVALID)
        return -1;
    /* These four setters report failure with their "invalid" value (0 for
     * the stop bits), never -1. (set_vmin and set_vtime do return -1.) */
    if (set_char_size(CHAR_SIZE_8) == CHAR_SIZE_INVALID)
        return -1;
    if (set_num_of_stop_bits(1) == 0)
        return -1;
    if (set_parity(PARITY_NONE) == PARITY_INVALID)
        return -1;
    if (set_flow_control(FLOW_CONTROL_NONE) == FLOW_CONTROL_INVALID)
        return -1;
    if (set_vmin(1) == -1)
        return -1;
    if (set_vtime(0) == -1)
        return -1;
    return 0;
}

/* --- unbuffered streambuf ------------------------------------------------ */

std::streamsize SerialStream::xsputn(const char *data, std::streamsize length)
{
    ssize_t written;

    if (fd_ == -1 || length < 1)
        return 0;
    written = ::write(fd_, data, length);
    if (written == -1 || written == 0)
        return 0;
    return written;
}

/* A byte is available if one is waiting in the putback slot, or if a
 * non-blocking read of one byte succeeds (it then waits in the slot). */
std::streamsize SerialStream::showmanyc()
{
    int flags;
    std::streamsize available;

    if (fd_ == -1)
        return -1;
    if (putback_available_)
        return 1;
    flags = fcntl(fd_, F_GETFL, 0);
    if (fcntl(fd_, F_SETFL, flags | O_NONBLOCK) == -1)
        return -1;
    available = (::read(fd_, &putback_char_, 1) == 1);
    if (available)
        putback_available_ = true;
    if (fcntl(fd_, F_SETFL, flags) == -1)
        return -1;
    return available;
}

int SerialStream::underflow()
{
    char c;
    ssize_t n;

    if (fd_ == -1)
        return EOF;
    if (putback_available_) {
        c = putback_char_;
    } else {
        n = ::read(fd_, &c, 1);
        if (n == 1) {
            putback_char_ = c;
            putback_available_ = true;
        } else if (n == -1 || n == 0) {
            return EOF;
        }
    }
    return (unsigned char)c;
}

int SerialStream::uflow()
{
    int c = underflow();

    putback_available_ = false;
    return c;
}

/* --- stream operations ---------------------------------------------------- */

SerialStream &SerialStream::operator<<(const char *text)
{
    std::streamsize length;

    if (!good())
        return *this;
    if (text == NULL) {
        setstate(BADBIT);
        return *this;
    }
    length = std::strlen(text);
    if (xsputn(text, length) != length)
        setstate(BADBIT);
    return *this;
}

SerialStream &SerialStream::write(const char *data, std::size_t length)
{
    if (!good())
        return *this;
    if (xsputn(data, length) != (std::streamsize)length)
        setstate(BADBIT);
    return *this;
}

/* No get area, so in_avail() is always showmanyc(). */
int SerialStream::in_avail()
{
    return showmanyc();
}

SerialStream &SerialStream::get(char &c)
{
    int next;

    if (!good()) {
        setstate(FAILBIT);
        return *this;
    }
    next = uflow();
    if (next == EOF)
        setstate(EOFBIT | FAILBIT);
    else
        c = (char)next;
    return *this;
}

/* --- settings ------------------------------------------------------------- */

speed_t SerialStream::set_baud_rate(speed_t baud)
{
    struct termios t;

    if (fd_ == -1)
        return BAUD_INVALID;
    /* B50..B38400, B57600 and B115200 only. */
    if ((int)baud < 1 || ((int)baud > B38400 && baud != B57600 && baud != B115200))
        return BAUD_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return BAUD_INVALID;
    cfsetispeed(&t, baud);
    cfsetospeed(&t, baud);
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return BAUD_INVALID;
    return baud_rate();
}

speed_t SerialStream::baud_rate()
{
    struct termios t;
    speed_t speed;

    if (fd_ == -1)
        return BAUD_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return BAUD_INVALID;
    speed = cfgetispeed(&t);
    if (speed != cfgetospeed(&t))
        return BAUD_INVALID;
    if ((speed >= B50 && speed <= B38400) || speed == B57600 || speed == B115200)
        return speed;
    return BAUD_INVALID;
}

int SerialStream::set_char_size(int size)
{
    struct termios t;

    if (fd_ == -1)
        return CHAR_SIZE_INVALID;
    if (size != CS5 && size != CS6 && size != CS7 && size != CS8)
        return CHAR_SIZE_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return CHAR_SIZE_INVALID;
    /* Strip the eighth bit of input unless characters are 8 bits wide. */
    if (size == CS8)
        t.c_iflag &= ~ISTRIP;
    else
        t.c_iflag |= ISTRIP;
    t.c_cflag = (t.c_cflag & ~CSIZE) | size;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return CHAR_SIZE_INVALID;
    return char_size();
}

int SerialStream::char_size()
{
    struct termios t;

    if (fd_ == -1)
        return CHAR_SIZE_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return CHAR_SIZE_INVALID;
    return t.c_cflag & CSIZE;
}

short SerialStream::set_num_of_stop_bits(short stop_bits)
{
    struct termios t;

    if (fd_ == -1)
        return 0;
    if (tcgetattr(fd_, &t) == -1)
        return 0;
    if (stop_bits == 1)
        t.c_cflag &= ~CSTOPB;
    else if (stop_bits == 2)
        t.c_cflag |= CSTOPB;
    else
        return 0;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return 0;
    return num_of_stop_bits();
}

short SerialStream::num_of_stop_bits()
{
    struct termios t;

    if (fd_ == -1)
        return 0;
    if (tcgetattr(fd_, &t) == -1)
        return 0;
    return (t.c_cflag & CSTOPB) ? 2 : 1;
}

int SerialStream::set_parity(Parity value)
{
    struct termios t;

    if (fd_ == -1)
        return PARITY_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return PARITY_INVALID;
    if (value == PARITY_ODD)
        t.c_cflag |= PARENB | PARODD;
    else if (value == PARITY_NONE)
        t.c_cflag &= ~PARENB;
    else if (value == PARITY_EVEN)
        t.c_cflag = (t.c_cflag & ~PARODD) | PARENB;
    else
        return PARITY_INVALID;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return PARITY_INVALID;
    return parity();
}

int SerialStream::parity()
{
    struct termios t;

    if (fd_ == -1)
        return PARITY_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return PARITY_INVALID;
    if (!(t.c_cflag & PARENB))
        return PARITY_NONE;
    return (t.c_cflag & PARODD) ? PARITY_ODD : PARITY_EVEN;
}

int SerialStream::set_flow_control(FlowControl value)
{
    struct termios t;

    if (fd_ == -1)
        return FLOW_CONTROL_INVALID;
    /* Pending data is thrown away before the change. */
    if (tcflush(fd_, TCIOFLUSH) == -1)
        return FLOW_CONTROL_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return FLOW_CONTROL_INVALID;
    if (value == FLOW_CONTROL_HARD) {
        t.c_iflag &= ~(IXON | IXOFF);
        t.c_cflag |= CRTSCTS;
        t.c_cc[VSTART] = 0;
        t.c_cc[VSTOP] = 0;
    } else if (value == FLOW_CONTROL_SOFT) {
        t.c_iflag |= IXON | IXOFF;
        t.c_cflag &= ~CRTSCTS;
        t.c_cc[VSTART] = XON;
        t.c_cc[VSTOP] = XOFF;
    } else {
        t.c_iflag &= ~(IXON | IXOFF);
        t.c_cflag &= ~CRTSCTS;
    }
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return FLOW_CONTROL_INVALID;
    return flow_control();
}

int SerialStream::flow_control()
{
    struct termios t;

    if (fd_ == -1)
        return FLOW_CONTROL_INVALID;
    if (tcgetattr(fd_, &t) == -1)
        return FLOW_CONTROL_INVALID;
    if ((t.c_iflag & IXON) && (t.c_iflag & IXOFF) && t.c_cc[VSTART] == XON && t.c_cc[VSTOP] == XOFF)
        return FLOW_CONTROL_SOFT;
    if (!(t.c_iflag & IXON) && !(t.c_iflag & IXOFF))
        return (t.c_cflag & CRTSCTS) ? FLOW_CONTROL_HARD : FLOW_CONTROL_NONE;
    return FLOW_CONTROL_INVALID;
}

short SerialStream::set_vmin(short vmin)
{
    struct termios t;

    if (fd_ == -1 || vmin < 0 || vmin > 255)
        return -1;
    if (tcgetattr(fd_, &t) == -1)
        return -1;
    t.c_cc[VMIN] = (cc_t)vmin;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return -1;
    return vmin;
}

short SerialStream::set_vtime(short vtime)
{
    struct termios t;

    if (fd_ == -1 || vtime < 0 || vtime > 255)
        return -1;
    if (tcgetattr(fd_, &t) == -1)
        return -1;
    t.c_cc[VTIME] = (cc_t)vtime;
    if (tcsetattr(fd_, TCSANOW, &t) == -1)
        return -1;
    return vtime;
}
