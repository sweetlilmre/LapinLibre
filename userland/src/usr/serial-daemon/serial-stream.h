/* SPDX-License-Identifier: MIT */
/*
 * SerialStream / SerialStreamBuf: the serial port stream serial-daemon
 * uses, built into it.
 *
 * The stream is an std::iostream over an unbuffered streambuf. Only the
 * operations the daemon uses are provided: the opening constructor, stream
 * state, insertion of a C string, write(), in_avail() and get().
 */
#ifndef SERIAL_STREAM_H
#define SERIAL_STREAM_H

#include <cstddef>
#include <string>

#include <termios.h>

/* The stream's enumerations; the values are what its code compares against. */
namespace serial {

/* Baud rates are the termios B* codes; BAUD_INVALID is none of them. */
const speed_t BAUD_DEFAULT = B57600;
const speed_t BAUD_INVALID = (speed_t)-1;

const int CHAR_SIZE_8 = CS8;
const int CHAR_SIZE_INVALID = CS8 + 1;

enum Parity { PARITY_EVEN, PARITY_ODD, PARITY_NONE, PARITY_INVALID };
enum FlowControl { FLOW_CONTROL_HARD, FLOW_CONTROL_SOFT, FLOW_CONTROL_NONE, FLOW_CONTROL_INVALID };

} // namespace serial

class SerialStream {
public:
    /* SerialStream(name, baud, char size, parity, stop bits, flow control):
     * opens the port read/write and applies each setting in turn. Failures
     * only leave the stream not good(); the descriptor of a port that opened
     * but could not be set up stays open until the stream is destroyed. */
    SerialStream(const std::string &name, speed_t baud, int char_size, serial::Parity parity, short stop_bits,
                 serial::FlowControl flow_control);
    ~SerialStream();

    bool good() const { return state_ == 0; }
    /* std::ios::clear(): good again (used by read_ports() on a stream gone bad). */
    void clear() { state_ = 0; }

    /* std::ostream insertion and write(): one write() call each. A short or
     * failed write makes the stream bad, after which nothing is written. */
    SerialStream &operator<<(const char *text);
    SerialStream &write(const char *data, std::size_t length);

    /* std::streambuf::in_avail(): 1 if a byte can be read without blocking. */
    int in_avail();

    /* std::istream::get(char &): fails (leaving c untouched) on a stream
     * that is not good. */
    SerialStream &get(char &c);

private:
    SerialStream(const SerialStream &);
    SerialStream &operator=(const SerialStream &);

    /* std::ios state bits, as libstdc++ numbers them. */
    enum { BADBIT = 1, EOFBIT = 2, FAILBIT = 4 };
    void setstate(int bits) { state_ |= bits; }

    /* SerialStream::Open / SerialStreamBuf::open and close. */
    void open(const std::string &name);
    bool buf_open(const std::string &name);
    bool buf_close();

    /* SerialStreamBuf internals. */
    int initialize_serial_port();
    int set_parameters_to_default();
    std::streamsize xsputn(const char *data, std::streamsize length);
    std::streamsize showmanyc();
    int underflow();
    int uflow();

    /* SerialStreamBuf setters and getters (SerialStream's setters wrap the
     * first five and turn their "invalid" results into a bad stream). */
    speed_t set_baud_rate(speed_t baud);
    speed_t baud_rate();
    int set_char_size(int char_size);
    int char_size();
    short set_num_of_stop_bits(short stop_bits);
    short num_of_stop_bits();
    int set_parity(serial::Parity parity);
    int parity();
    int set_flow_control(serial::FlowControl flow_control);
    int flow_control();
    short set_vmin(short vmin);
    short set_vtime(short vtime);

    int fd_;                 /* -1 when closed */
    bool putback_available_; /* a byte read by showmanyc() waits in putback_char_ */
    char putback_char_;
    int state_;
};

#endif
